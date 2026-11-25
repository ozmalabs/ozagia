# MCP tools

> **Generated file.** Built by `scripts/docs/generate_docs.py` from `src/ozagia/integrations/mcp/server.py`, `src/ozagia/integrations/mcp/graph_surface.py`, `src/ozagia/integrations/mcp/sre.py`, `src/ozagia/research/mcp.py` (ozagia `0.1.5.dev0`). Do not edit by hand: change the source code and regenerate.

The MCP server (`ozagia mcp` / `ozagia-mcp`) exposes the graph as tools for any MCP client. The tool set depends on the mode (`--mode coding|research|combined`, default `coding`) and, for coding, the surface (`--surface full|graph`, default `full`).

| surface | tools | page |
|---|---|---|
| coding (default) | 89 | [coding.md](coding.md) |
| research | 107 | [research.md](research.md) |
| combined | 195 | [combined.md](combined.md) |
| graph (coding/combined with `--surface graph`) | 12 | [graph.md](graph.md) |

A project's `[adapters]` configuration can add further tools at runtime; those are not part of the built-in surface.
