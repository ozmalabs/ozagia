"""Kiro CLI hooks and MCP server entry.

See docs/kiro.md. `hook` runs one hook event; `install` writes the hooks
into `.kiro/hooks/ozagia.json` (the V3 engine) and/or an agent profile in
`.kiro/agents/` (every engine, when that agent is used), and the MCP server
into `.kiro/settings/mcp.json` (or the user-level files under `~/.kiro`).
"""
