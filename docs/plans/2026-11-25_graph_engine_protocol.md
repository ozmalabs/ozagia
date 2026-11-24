# Graph engine protocol (first slice)

Status: implemented 2026-11-25 in `src/ozagia/core/graph_engine.py`, with the
MCP surface in `src/ozagia/integrations/mcp/server.py` and tests in
`tests/test_graph_engine.py`.

This is the first slice of one compact, stateful, token-efficient graph
interface for LLMs. It replaces a family of independent `dev_*` calls with one
MCP tool (`graph`), one versioned session protocol, explicit token budgets,
`known` deltas and bounded speculation. It reuses the existing services rather
than re-implementing graph logic:

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
- **No true background overlap.** Speculation is synchronous: suggested work is
  planned and at most two bounded payloads are prepared *before* the response
  is returned. There is no worker and no overlap with a model turn.
- **No speculative writes.** A prepared payload is a read-only graph answer.
- **No edit/create/check/refresh/shell ops.** Those remain on the graph
  surface; the engine deliberately cannot reach them.
- **No multi-repository federation.** A session belongs to one loaded snapshot.
- **No persistent session store.** Sessions live in the server process and are
  bounded; restarting the server discards them.
- **No hit-rate measurement yet.** The prototype measurement in
  `docs/results/graph_engine_prototype_2026-11-25.md` counts tokens and
  round-trips, not model-task gains or speculation hit rate.

## Versioning and transport

- Protocol string: `ozagia.graph_engine/1`
- Protocol version: `1.0`
- MCP tool: `graph`
- MCP surfaces: `OZAGIA_SURFACE=engine` or `--surface engine`
- The tool's input schema has `additionalProperties: false`.
- JSON-RPC framing, errors and `tools/list` are the existing MCP server's.
  A bad tool argument is a JSON-RPC `-32602`; a bad engine request is a normal
  JSON result with `ok: false` and `err`.

A version bump is required for a wire-incompatible change: op removal, a
column meaning change, a new required input, or a change to the meaning of a
handle, hash or freshness label.

## Handshake

```json
{"op": "hello", "budget": 1200, "compact": true}
```

```json
{
  "ok": true,
  "op": "hello",
  "proto": "ozagia.graph_engine/1",
  "ver": "1.0",
  "sid": "s1",
  "caps": ["find", "outline", "expand", "impact", "tests", "context",
           "shape", "fetch"],
  "feats": ["compact", "known", "spec", "fetch"],
  "snap": {"id": "3f2a…", "fresh": "fresh", "head": "…", "gen": "…",
           "nodes": 1234, "edges": 5678},
  "budget": {"def": 1200, "max": 8000, "cur": 1200},
  "compact": true,
  "sl": {"prepared": 0, "fetched": 0, "wasted": 0, "evicted": 0}
}
```

Calling `hello` again without `session` creates a new isolated session. With a
live `session` it refreshes freshness and the budget rather than creating a
second handle. `hello` may report `fresh`, `stale` or `unknown`; it contains no
structural rows, so it is safe to answer even when the snapshot is not usable.

## Common response envelope

```json
{
  "ok": true,
  "op": "find",
  "snap": "3f2a…",
  "fresh": "fresh",
  "budget": 1200,
  "cols": ["k", "n", "f", "sp", "sig", "sum", "sc"],
  "rows": [["sym:pkg/core.py::add", "add", "pkg/core.py", [4, 4],
            "def add(a, b):", "Add two numbers.", 0.03]],
  "tok": 81,
  "truncated": false,
  "omitted": 0,
  "next": {"op": "find", "q": "add", "session": "s1", "offset": 20}
}
```

`ok`, `op`, `fresh`, `tok` and `budget` are stable fields. `snap`, `cols`,
`rows`, `truncated`, `omitted`, `next`, `unchanged`, `spec`, `ready`, `pending`
and `sl` appear when they apply. `tok` is the reported estimate for the whole
response (`len(json.dumps(response))` / 4); it is an estimate, not a
tokenizer measurement.

Once a response is stale or unverifiable:

```json
{"ok": false, "op": "find", "snap": "…", "fresh": "stale",
 "withheld": true, "err": "snapshot is not verifiably fresh; structural data is withheld",
 "tok": 29}
```

No `cols`, `rows`, `unchanged` or speculative payload is returned.

## Operations

| Op | Inputs | Result rows |
| --- | --- | --- |
| `hello` | `session?`, `budget?`, `compact?` | handshake, no graph rows |
| `find` | `q`, `kinds?`, `limit?`, `offset?` | ranked keys, names, files, spans, signatures, summaries, scores |
| `outline` | `key`, `limit?`, `known?` | a root row and (for a file) its defined symbols |
| `expand` | `keys`, `edges?`, `depth?`, `limit?`, `offset?` | nodes plus edge triples |
| `impact` | `keys`, `depth?`, `limit?` | symbols/files/tests with depth and one `why` path |
| `tests` | `key` or `keys`, `limit?` | covering tests and why each was linked |
| `context` | `task`, `limit?`, `offset?`, `mode?`, `known?` | cited task context items |
| `shape` | `limit?` | bounded repository identity/counts/languages/modules/commands |
| `fetch` | `fetch` (1–4 handles) or `h` | prepared speculative payloads |

`tests` discovers test keys; it never runs pytest. `shape` is a compact
projection of the deterministic `repo_shape` report.

## Compact encoding

Rows are arrays in the order of `cols`. Short keys mean:

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
| `e` | edges | neighbour counts by type |
| `r` | role | `root` or `child` |
| `g` | group | impact group (`s`, `f`, `t`) |
| `d` | depth | impact depth |
| `why` | why | one edge path or link explanation |
| `txt` | text | bounded source/outline text |
| `sec` | section | `repo`, `count`, `lang`, `module`, `cmd`, `conv` |
| `v` | value | shape value |

`verbose: true` expands each row into a named object using the long names. It is
for debugging, not for normal model context, and it is still budgeted.

Node handles are the existing stable keys: `file:<path>`,
`sym:<path>::<qualname>`, `test:<path>::<qualname>`, `doc:<path>`,
`text:<path>#L<a>-L<b>`, `repo:` and adapter keys. They are not per-response
integer ids.

## Sessions

One server process holds at most **16** live sessions (LRU). Evicting a
session counts its unfetched prepared payloads as wasted and its cache entries
as evicted. A request naming an evicted or unknown handle returns
`ok: false, err: "unknown or expired session; call hello"`.

A session stores:

- the last observed snapshot token and freshness label;
- up to 1,000 `key -> hash` entries remembered from responses;
- the last cursor/page position (today the self-contained `next` object is
  authoritative; the cursor is session bookkeeping);
- the last response token estimate and cumulative estimated tokens;
- the compact flag and current default budget;
- an ordered speculation cache (at most 8 entries) and a ledger.

## `known` deltas

Callers send outlines/context they already have:

```json
{"op": "outline", "session": "s1", "key": "sym:pkg/core.py::add",
 "known": [{"key": "sym:pkg/core.py::add", "hash": "1a2b3c4d5e6f7a8b"}]}
```

The engine hashes each item it would serialize. If the key and hash match, the
item is represented only by its key in `unchanged`:

```json
{"ok": true, "op": "outline", "unchanged": ["sym:pkg/core.py::add"], ...}
```

Changed, missing or unknown hashes are reserialized with a fresh `h`. A
16-hex SHA-256 prefix is used to keep the wire small; a collision would cause
the client to keep an old payload, so this is a prototype trade-off, not a
cryptographic identity guarantee.

## Token budgets

- Default: **1,200** estimated tokens.
- Maximum: **8,000**.
- Minimum accepted: **64**; lower requests are clamped up.
- Per-session default from `hello`; a per-call `budget` overrides it.
- The engine trims the *payload*, never silently overflows. Long text cells are
  shortened first; then lists are cut from the end.
- A cut list sets `truncated: true`, an `omitted` count and, when a continuation
  is meaningful, a self-contained `next` request with an `offset`.
- Optional metadata (`total`, `budget`, `next`) is dropped before rows, and
  `fresh` is never dropped. If even the metadata cannot fit, the response
  becomes `{ok, op, fresh, truncated}`.
- `tok` is computed after the final trim and is the value callers should trust
  for accounting. Prepared speculative payloads have their own bound
  (`SPEC_BUDGET = 400` estimated tokens, or the request budget if smaller).

## Speculation lifecycle

When a response has `spec: true`:

1. The engine derives up to six candidate requests from the returned keys:
   callers and callees (`expand`), covering tests (`tests`) and an outline
   (`outline`).
2. It skips a candidate that repeats the current request.
3. It prepares **at most two** candidates synchronously, each with a bounded
   budget, before returning. They are stored in the session cache and returned
   as handles.
4. The response reports `spec: ["q1","q2"]`, `ready: 2`, `pending: N` and the
   session ledger `sl`.

`fetch` (one handle, or up to four) returns the prepared payload without
repeating graph work. The entry is removed after the first fetch. A handle that
is unknown, evicted or prepared under a different snapshot is refused. If the
snapshot becomes stale or unknown, all prepared entries are invalidated.

Ledger counters:

| Counter | Meaning |
| --- | --- |
| `prepared` | payloads successfully prepared |
| `fetched` | prepared payloads actually fetched |
| `wasted` | prepared but never fetched when evicted/invalidated |
| `evicted` | cache entries removed (fetched or wasted) |

The cache is bounded at 8 entries per session. The engine also keeps
process-lifetime aggregate counters in `GraphEngine.stats()`.

**This slice does not overlap work with a model.** Preparation happens before
the response is returned; it can make the response slower but cannot make it
faster. The next step is a daemon/process worker that prepares after returning
and a hit-rate evaluation.

## Freshness and unknowns

Before any structural op, the engine asks `DevMemory.staleness(force=True,
allow_project_adapters=False)`. Project adapters are **not** loaded: freshness
must not execute project code. If the working tree, HEAD or root differs from
the snapshot, freshness is `stale`; if the check cannot be completed (no git,
no recorded digest/root/head, unknown adapter inputs, digest error), it is
`unknown`. Neither state answers with structural rows. A session snapshot token
change invalidates prepared speculation and updates the token on the next fresh
answer.

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
| prepared synchronously per response | 2 |
| handles per `fetch` | 4 |
| default response budget | 1,200 estimated tokens |
| maximum response budget | 8,000 |
| minimum accepted budget | 64 |
| prepared payload budget | 400 (or the request budget, if smaller) |
| rows requested by an op | 200 |

## Open work

- Daemon/process worker for true background speculation.
- Hit-rate and waste evaluation on larger repositories; tune the two-handle
  and 400-token defaults against measured fetches.
- Optional Ozma transport (the protocol values are JSON-ready and
  transport-neutral).
- A persistent, versioned session/cache layer if the process-local bound
  proves too short for long agent sessions.
- Stronger item identity than a 16-hex hash prefix if cross-process caching is
  added.
