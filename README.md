# ozagia

**Ozma advanced graph integration agent.** A live code graph for LLM coding
agents: index a repository into typed nodes (files, symbols, tests, docs,
imports, call edges), keep it current while the agent edits, and put it in the
agent's path. MIT licensed.

Status: alpha. The library, the MCP server, the OpenCode capture plugin and
the agent harnesses work; the Claude Code hooks are untested against a live
Claude Code. Measurements are in `docs/results/`; plans in `docs/plans/`.

## Install

```bash
curl -fsSL https://ozagia.com/install | bash
cd /path/to/your/repo
ozagia                     # OpenCode's TUI with the ozagia graph in its path
```

The installer puts a self-contained ozagia (its own Python, with the
tree-sitter parsers) and a pinned OpenCode release under `~/.ozagia`, checks
every download's sha256, and adds `~/.ozagia/bin` to PATH. Linux (x64, arm64,
glibc) and macOS (arm64, x64). `ozagia self check --tui` verifies the install;
`ozagia self upgrade` and `ozagia self uninstall` manage it. See
[docs/install.md](docs/install.md); releases are made as in
[docs/release.md](docs/release.md).

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
| MCP server | `ozagia mcp`: any MCP client (OpenCode, Claude Code, Cursor, ...) gets the graph tools next to its own | works |
| OpenCode plugin | captures `read`/`grep`/`glob`/`edit`/`write`/`shell` and carries out their intent on the graph daemon; `ozagia install opencode` | works (OpenCode 2.3, live smoke runs) |
| Claude Code hooks | partial capture: graph context after each tool, git ref refusal; `ozagia install claude` | implemented, untested live |
| Harness | `ozagia agent`: an OpenCode session with the daemon and plugin set up and torn down for you ([docs/harness.md](docs/harness.md)); `python -m ozagia.harness.native`: a small agent loop over any OpenAI-compatible API, with or without the graph ([docs/native_loop.md](docs/native_loop.md)) | implemented; the eval's live runs use the same pieces |

## Quick start (from source)

```bash
uv pip install -e '.[all]'          # or: pip install -e '.[all]'
cd /path/to/your/repo
ozagia index                        # builds or refreshes .ozagia/graph.json
ozagia-memory search "retry backoff"
ozagia-memory context "make the retry loop give up after five attempts"
```

Add `.ozagia/` to the repository's `.gitignore`.

OpenCode with the graph in its path (needs `opencode` on PATH):

```bash
ozagia agent --prompt "make tests/test_retry.py pass" -m vllm/qwen3.8-27b
ozagia agent --clone --prompt-file task.txt   # in a throwaway clone of HEAD
ozagia agent                                  # the interactive TUI
```

Each session builds or refreshes the snapshot, starts the graph daemon,
installs the plugin, runs OpenCode, and removes the plugin and stops the
daemon afterwards, also on Ctrl-C. See [docs/harness.md](docs/harness.md).

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

## Research mode

Use `ozagia-mcp --mode research --root /path/to/workspace` for source-backed
research graphs, or `--mode combined` to expose research and coding tools
together. Coding mode remains the default. See [research mode](docs/research.md)
for setup, optional dependencies and model/Lean limitations.

## Origin

ozagia was extracted, with its history, from the Ocelot repository, where it
was built as Ocelot's dev tooling. Most docs under `docs/` still use Ocelot as
the example repository, and `docs/results/` holds the measurements made on
Ocelot.

## Development

See `AGENTS.md`.
