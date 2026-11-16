# Changelog

## Unreleased

First release.

- One-shot install: `curl -fsSL https://ozagia.com/install | bash` puts a
  self-contained ozagia (bundled CPython and tree-sitter parsers) and a
  pinned OpenCode under `~/.ozagia`. `ozagia` alone opens OpenCode's TUI with
  the graph daemon and plugin; `ozagia self info|check|upgrade|uninstall`
  manage the install.
- Graph index, retrieval, graph-addressed edits, MCP server, graph daemon and
  OpenCode capture plugin (read, grep, glob, edit, write, shell), including
  compound shell commands carried out step by step, and OpenCode permission
  checks before every handled call.
- Agent harnesses: `ozagia agent` (OpenCode) and the native loop over any
  OpenAI-compatible API. Evals: retrieval, break/fix agent eval, index bench.
- Claude Code hooks (untested against a live Claude Code).
