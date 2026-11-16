# Changelog

## 0.1.0 (2026-11-16)

First release.

- One-shot install: `curl -fsSL https://ozagia.com/install | bash` puts a
  self-contained ozagia (bundled CPython, tree-sitter parsers, PyYAML, SymPy
  and Lark) and a
  pinned OpenCode under `~/.ozagia`. `ozagia` alone opens OpenCode's TUI with
  the graph daemon and plugin; `ozagia self info|check|upgrade|uninstall`
  manage the install.
- Graph index, retrieval, graph-addressed edits, MCP server, graph daemon and
  OpenCode capture plugin (read, grep, glob, edit, write, shell), including
  compound shell commands carried out step by step, and OpenCode permission
  checks before every handled call.
- Agent harnesses: `ozagia agent` (OpenCode) and the native loop over any
  OpenAI-compatible API. Evals: retrieval, break/fix agent eval, index bench.
- Proactive task context: with OpenCode's session context hook, the plugin
  puts a small, source-attributed graph hint before each model call (fresh
  snapshots only, capped at 1,600 characters).
- Research mode (`ozagia mcp --mode research|combined`, `ozagia agent
  --research`, `ozagia-research`): a source-backed research ledger with
  paper, PDF, TEI, BibTeX/CSL/RIS and document ingestion; open-access
  reference resolution and fetch (arXiv, Crossref, OpenAlex, Unpaywall,
  Europe PMC); citation graphs; full-text and optional semantic search;
  screening, extraction, risk-of-bias and certainty records; contradiction
  and retraction alerts; structured multi-model paper review with
  agreement, reconciliation and steelman/red-team arguments; theory
  comparison; manuscript checks and bundles; formula interpretation and
  Lean export.
- Infrastructure context (experimental, opt-in adapter): source-level
  Terraform/OpenTofu, Kubernetes, Helm, Kustomize, CloudFormation, ARM,
  Pulumi YAML, CDK assemblies and GitOps facts in the graph, `infra_*` MCP
  tools (context, locate, dependencies, impact, status, SARIF and CDK
  import), and infrastructure hints in the proactive context.
- Explicit API mappings over indexed OpenAPI operations, and a read-only
  context provider for Ozma.
- Claude Code hooks (untested against a live Claude Code).
