# ozagia

**Ozma advanced graph integration agent.** A live code graph for LLM coding
agents: index a repository into typed nodes (files, symbols, tests, docs,
imports, call edges), keep it current while the agent edits, and put it in the
agent's path. MIT licensed.

Status: alpha. The library and the MCP server work today; the tool-capture
plugin and the agent harness are planned (see `docs/plans/`).

## What it does

- **Graph index**: files, symbols, tests, docs, imports, git history and known
  failures as one graph snapshot, with project-specific nodes added by
  adapters.
- **Retrieval**: ranked repo map (PageRank over references), fused lexical and
  graph code search, symbol-level callers, callees and impact, architecture
  summary. Optional language-server refinement (pyright, rust-analyzer).
- **Graph-addressed edits**: Python symbol and AST ops, Rust/Lua CST ops,
  Markdown section ops, JSON path ops. Preview by default, base hash check,
  atomic write, backup. A diff is output only, never input.
- **Evals**: a leakage-checked retrieval eval, a break/fix agent eval with
  selectable agent backends (OpenCode, Claude Code), and an index bench.

## Ways to use it

| depth | how | status |
|---|---|---|
| Library | `from ozagia.core.memory import DevMemory, build_index` | works |
| MCP server | any MCP client (OpenCode, Claude Code, Cursor, ...) gets the graph tools next to its own | works |
| OpenCode plugin | captures `read`/`grep`/`edit`/`shell` and carries out their intent on the graph | planned (verified feasible on OpenCode 2.3) |
| Claude Code hooks | partial capture: rewrite or block calls, add graph context | planned |
| Harness | `ozagia agent`: a full agent session on a repository | planned |

## Quick start

```bash
uv pip install -e '.[all]'          # or: pip install -e '.[all]'
cd /path/to/your/repo
ozagia-memory build                 # writes .ozagia/graph.json
ozagia-memory search "retry backoff"
ozagia-memory context "make the retry loop give up after five attempts"
```

Add `.ozagia/` to the repository's `.gitignore`.

MCP (OpenCode `opencode.json`):

```json
{
  "mcp": {
    "ozagia": {
      "type": "local",
      "command": ["/path/to/venv/bin/python", "-m", "ozagia.integrations.mcp.server"],
      "environment": {"OZAGIA_ROOT": "/path/to/your/repo"}
    }
  }
}
```

## Configuring a repository

Without configuration ozagia indexes the whole checkout, skipping the usual
build and cache directories. A `.ozagia.toml` at the root narrows the source
directories, sets the docs to index, extra import roots and skip names, and
names adapters. The full schema is in `src/ozagia/config.py`.

```toml
[index]
source_dirs = ["src", "tools"]
test_dirs = ["tests"]
docs = ["README.md", "docs/**/*.md"]

[adapters]
modules = ["tools.my_adapter:register"]
```

An adapter is a `register(registry)` function that adds indexers (extra node
and edge types), edit engines, MCP tools or graph queries. See
`src/ozagia/adapters.py`. Ocelot's adapter
(`tools/ozagia_ocelot_adapter.py` in https://github.com/ozmalabs/ocelot) is a
worked example: it adds seed manifests and rules as graph nodes.

## Origin

ozagia was extracted, with its history, from the Ocelot repository, where it
was built as Ocelot's dev tooling. Most docs under `docs/` still use Ocelot as
the example repository, and `docs/results/` holds the measurements made on
Ocelot.

## Development

See `AGENTS.md`.
