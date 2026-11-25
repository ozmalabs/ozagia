# Ozma context integration

Ozagia provides a read-only Python boundary for an Ozma context assembler or
another local client. Install ozagia in the caller's environment and build the
snapshot separately with `ozagia index --root /path/to/repo`. The provider
never indexes the repository, imports its code, or starts a model.

```python
from pathlib import Path
from ozagia.core.memory import DevMemory
from ozagia.integrations.ozma.context import task_context

root = Path('/path/to/repo')
memory = DevMemory(dump=root / '.ozagia/graph.json',
                   journal=root / '.ozagia/journal.jsonl', root=root)
result = task_context(memory, 'find the retry loop', budget_tokens=1500)
if result['ok']:
    items = result['items']  # snippets and source paths
else:
    items = []  # caller can fall back to its existing retrieval path
```

Version 1 is a Python contract. The optional local HTTP transport described
below exposes the same provider for a client that cannot import ozagia; it is
not full Ozma protocol conformance. It rejects stale or unverifiable snapshots
by default; `allow_stale` permits them and preserves the freshness label.
Freshness is a point-in-time Git-based check and does not lock the worktree. It
includes untracked and Git-ignored source, test, selected documentation, and
known adapter-owned paths. Newly added ignored adapter inputs outside configured
roots cannot be discovered until an adapter or rebuild records them. Snapshots
made with an older digest may need a rebuild. The snippet text budget is a
four-characters-per-token approximation; JSON and metadata are outside it.
Callers must account for their full prompt, arrange their own timeout, and
choose whether to include output. Relationships are graph evidence, not proof
of semantic correctness. Compare retrieval quality and end-to-end outcomes
before enabling it by default.

## Optional local HTTP transport

For a local caller that cannot import ozagia, `ozagia engine serve` exposes the
same read-only provider plus the stateful graph engine over loopback HTTP/JSON:

```bash
ozagia engine token --root /path/to/repo     # mode 0600; prints path+fingerprint
ozagia engine serve --root /path/to/repo --port 8765
```

```bash
curl -sS http://127.0.0.1:8765/v1/ozma/context \
  -H "Authorization: Bearer $OZAGIA_ENGINE_TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"task": "find the retry loop", "budget_tokens": 1500,
       "max_items": 8, "allow_stale": false}'
```

The response is the provider's versioned result (`version: 1`) with `ok`,
`freshness`, `snapshot`, budget metadata and snippets; a stale snapshot is
refused unless `allow_stale` is true. The same process also serves
`POST /v1/graph` with the engine wire protocol, and `GET /healthz` is liveness
only. A bearer token from `OZAGIA_ENGINE_TOKEN` or `<state>/engine/token` is
required; there is no unauthenticated `/v1` path, CORS or directory serving.
The server binds `127.0.0.1` by default and caps bodies at 64 KiB and
concurrent calls at four. `--allow-remote` is an explicit opt-in, not a remote
mode: HTTP is unencrypted and the caller owns TLS and the network/auth
perimeter. A Python caller that can import ozagia should use
`ozagia.engine_client.GraphEngineClient`, which owns the session and injects
the op-scoped `known` deltas automatically; see [the client
example](graph_engine.md#python-client-ozma-context-assembler).

For the provider specifically, `GraphEngineClient.task_context(task,
budget_tokens=1500, max_items=8, allow_stale=False)` posts to
`/v1/ozma/context` and returns the same versioned result as the Python
provider. It is session-independent: it does not call `hello`, does not send or
create a `session`, and does not read or write remembered `known` hashes, so an
assembler can use one client for graph operations and provider context (or use
a fresh client with no graph session at all). The method validates
`task`/`budget_tokens`/`max_items`/`allow_stale` before sending and maps
`invalid_context_request` to `EngineContextError`; see [the client
reference](graph_engine.md#ozma-task-context).
`examples/ozma_context_client.py` is a runnable offline example: it starts a
loopback server for an indexed checkout, calls `task_context`, and prints only
metadata (never the token or snippets).

This HTTP path is **optional local transport for the existing provider and
engine**. It does not implement the full Ozma protocol, an Ozma server, a
discovery/handshake contract, authentication beyond a local bearer token, or a
network-safe remote deployment. The Python `task_context` provider remains the
canonical contract; see [The `graph` engine
surface](graph_engine.md#http-transport-optional) for the transport details and
bounds.

## Selected API specifications

An adapter may call `ozagia.core.openapi.index_openapi_document` with a graph
builder, file node, repository-relative path and JSON or YAML text. YAML
requires `uv pip install --python .venv/bin/python 'ozagia[openapi-yaml]'`
(or `python -m pip install 'ozagia[openapi-yaml]'`); aliases and unsupported tags
are rejected, and source size and structure are bounded. It adds API
operation and schema nodes with JSON Pointer locations, and edges for direct
schema references inside operations. It reports unresolved, external and
unsupported references found within operations. It does not automatically
scan files, resolve path-level or transitive references, verify
full OpenAPI compliance, or infer implementations, consumers or tests. The
adapter owns file selection; reingest can refresh tracked adapter inputs.
See `tests/test_openapi_graph.py` for the input contract and
`tests/test_openapi_adapter_integration.py` for a build and reingest example.

Run `ozagia doctor --root /path/to/repo --json` to inspect the local snapshot,
configuration and parser availability. It reports whether language-server
binaries are present; it does not start them or establish server readiness.
The command does not build a graph or load project adapters.

The Ozma backend's assembler supports this provider behind
`OZMA_OZAGIA_CONTEXT=1` or `assemble(..., use_ozagia=True)`. A separate
isolated Python process loads each repository's configuration; configure
`OZMA_OZAGIA_PYTHON` when Ozagia is installed in a different environment.
At most four workers run concurrently, with a busy fallback. Timeout terminates
the worker. The default is five seconds; `OZMA_OZAGIA_CONTEXT_TIMEOUT_SECONDS`
accepts 0.05–30 seconds, and the per-call argument overrides it. The provider checks freshness on both sides of context assembly;
this detects ordinary concurrent changes but is not a filesystem lock.

See [API mappings](api-mappings.md) for explicit, provenance-bearing links
from producer, consumer, and test locations to indexed OpenAPI operations.
