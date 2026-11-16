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

Version 1 is a Python contract rather than an HTTP endpoint. It rejects stale or unverifiable snapshots by default; `allow_stale`
permits them and preserves the freshness label. Freshness is a point-in-time
Git-based check and does not lock the worktree. It includes untracked and
Git-ignored source, test, selected documentation, and known adapter-owned
paths. Newly added ignored adapter inputs outside configured roots cannot be
discovered until an adapter or rebuild records them. Snapshots made with an
older digest may need a rebuild. The snippet text budget is a
four-characters-per-token approximation; JSON and metadata are outside it.
Callers must account for their full prompt, arrange their own timeout, and
choose whether to include output. Relationships are graph evidence, not proof
of semantic correctness. Compare retrieval quality and end-to-end outcomes
before enabling it by default.

## Selected API specifications

An adapter may call `ozagia.core.openapi.index_openapi_document` with a graph
builder, file node, repository-relative path and JSON or YAML text. YAML
requires `pip install 'ozagia[openapi-yaml]'`; aliases and unsupported tags
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
