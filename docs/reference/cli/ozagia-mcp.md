# `ozagia-mcp`

> **Generated file.** Built by `scripts/docs/generate_docs.py` from `src/ozagia/integrations/mcp/server.py` (ozagia `0.1.5.dev0`). Do not edit by hand: change the source code and regenerate.

This command's module does not expose a `build_parser()`;
its usage synopsis comes from the module docstring:

```
ozagia.integrations.mcp.server — stdio MCP server over the dev-memory snapshot.

An existing coding agent (OpenCode, Claude Code, Cursor, any MCP client) gets
the repository's graph memory as tools. The agent decides; the tools only
answer structural questions and record decisions/verification receipts.

`dev_seed` and `dev_rule` are listed only when the snapshot has `Seed` (resp.
`Rule`) nodes, which come from an adapter indexer. Adapters can add tools
(`Registry.add_mcp_tool`, see `ozagia.adapters`).

It is deliberately stdlib-only (no `mcp` package dependency) and READ-ONLY over
the graph, with one small append-only journal for decisions and verifications.
The graph itself is built by `ozagia.core.memory`.

Run (stdio):

    .venv/bin/python -m ozagia.integrations.mcp.server

Environment:
    OZAGIA_DUMP     snapshot path (default .ozagia/graph.json)
    OZAGIA_JOURNAL  journal path  (default .ozagia/journal.jsonl)
    OZAGIA_AUTOBUILD  "1" to build the snapshot on first start if absent
    OZAGIA_ROOT     checkout the tools act on (default: the current Git top level)
    OZAGIA_SURFACE  "full" (default) or "graph" (the find/node/expand/impact/
                    edit/create/check/note verbs); `--surface` overrides it
```
