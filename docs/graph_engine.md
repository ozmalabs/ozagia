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

The reply carries:

- the protocol (`ozagia.graph_engine/1`) and version;
- a session handle (`sid`) and capabilities;
- snapshot identity and freshness;
- default (1,200) and maximum (8,000) estimated token budgets;
- the session speculation ledger.

Pass `session` on every later request. Sessions are isolated and bounded to 16
per server process (LRU); an evicted handle must call `hello` again.

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
| `fetch` | a prepared speculative payload |

Example:

```json
{"op": "find", "session": "s1", "q": "retry backoff", "budget": 800}
```

```json
{"op": "impact", "session": "s1",
 "keys": ["sym:pkg/retry.py::backoff"], "depth": 2, "budget": 600}
```

Rows are compact arrays; `cols` names the fields (`k` key, `f` file, `sp`
source span, `sig` signature, `sum` summary, `why` reason, `txt` text, ...).
`verbose: true` expands rows to named objects for debugging.

## Budgets and paging

Every result reports `tok`, an estimated response size. `budget` is a hard
estimated-token limit (default 1,200, maximum 8,000, minimum 64). When the
result does not fit:

- long text is shortened first, then rows are cut;
- `truncated` and `omitted` are set;
- `next` (when useful) is a complete follow-up request with the next `offset`.

```json
{"op": "find", "session": "s1", "q": "pkg", "budget": 100}
```

## `known` deltas

If you already have an outline or context item, send its `{key, hash}`:

```json
{"op": "outline", "session": "s1", "key": "sym:pkg/core.py::add",
 "known": [{"key": "sym:pkg/core.py::add", "hash": "1a2b3c4d5e6f7a8b"}]}
```

Matching items come back only as key handles in `unchanged`; changed or missing
hashes are reserialized with a new `h`.

## Speculation

Set `spec: true` to let the engine suggest next work from the current result:

```json
{"op": "find", "session": "s1", "q": "add", "spec": true}
```

It returns up to two prepared handles (`spec`), `ready`, `pending` and the
session ledger (`prepared`, `fetched`, `wasted`, `evicted`). Fetch one or more:

```json
{"op": "fetch", "session": "s1", "fetch": ["q1"]}
```

This slice prepares **before returning**; it does not yet run a background
worker or overlap with model work. Prepared payloads are bounded (400 estimated
tokens by default) and are invalidated when the snapshot changes or becomes
stale/unknown.

## Freshness and safety

The engine refuses structural data when the snapshot is `stale` or `unknown`:

```json
{"ok": false, "op": "find", "fresh": "stale", "withheld": true,
 "err": "snapshot is not verifiably fresh; structural data is withheld"}
```

It is read-only: no edit, create, test execution, refresh, shell, target import,
network or model call. Sessions and speculation live in memory and are not
written to the checkout.

## Current limits

- Process-local sessions; a restart loses them.
- No true background speculation or hit-rate measurement yet.
- A compact 16-hex content hash for `known`, not a cryptographic identity.
- One snapshot per session; federation is not part of this surface.
- A prototype token/round-trip measurement, not a model-task gain:
  [`docs/results/graph_engine_prototype_2026-11-25.md`](results/graph_engine_prototype_2026-11-25.md).
