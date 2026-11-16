# Changelog

## Unreleased

- Graph-addressed goals (`ozagia.core.goals`, `ozagia goal
  add|list|show|check|claim|drop|block|from-review`, MCP `goal_list`,
  `goal_show`, `goal_check`, `goal_add`, `goal_claim`): repo-level goals in
  `.ozagia/goals/goals.json` whose targets, scope and criteria are graph
  keys, resolved at creation. Criteria (`test_passes`, `test_fails`,
  `symbol_exists`, `symbol_absent`, `calls`, `not_calls`, `no_callers`,
  `file_exists`, `file_absent`, `unchanged`, `lint_clean`,
  `review_resolved`, `ops_has`) are evaluated from the graph, the working
  tree and the test record, each to pass/fail/stale/unknown with evidence.
  The daemon has a read-only `goals` op, names a claimed goal after an edit
  that touches its targets, and adds an advisory note for edits outside a
  claimed goal's scope; the session brief and the compaction context list
  open and claimed goals with unmet criteria. The test record
  (`working_set/tests.json`) now also keeps the last 50 runs (time,
  selectors, failed keys, completeness), so a test's last pass has a time.
  See docs/goals.md.

- Lint inside the edit: after a handled edit, key edit or write, and in
  `after` for a built-in edit, write or Codex patch, the configured linter
  (ruff when the project configures it, `rustfmt --check` for Rust crates)
  runs on the changed files with a 3 s budget, and its findings on the
  changed lines come back as `[ozagia] lint <rule> L<n> <key>: <message>`
  lines (at most 10). Configure with `[lint] enabled`, `timeout_s`,
  `python`, `rust`. Linters never modify files. `ruff check [PATH...]
  [--select X] [--output-format concise|full]` (also `.venv/bin/ruff`) is a
  captured shell class: ruff's own output plus the node key of each
  finding. New stats: `lint_runs`, `lint_findings`, `lint_timeouts`,
  `ruff_captures`. Lint runs no program from inside the repository unless
  the user opts in outside it (`OZAGIA_LINT_TRUST_VENV=1` or
  `~/.config/ozagia/config.toml` `[lint] trust_project_venv = true`), runs
  with the graph lock released and the captured-pytest environment, and
  its budget is capped at 10 s and ends before the request deadline.
  See docs/intent.md.

- Kiro CLI integration: `ozagia install kiro [--scope project|user]
  [--format v3|agent|both]` writes command hooks to `.kiro/hooks/ozagia.json`
  (the default; what kiro-cli 2.37's V3 engine loads) and/or an agent
  profile `.kiro/agents/ozagia.json` (every engine, with
  `--agent ozagia`), and the ozagia MCP server in `.kiro/settings/mcp.json`;
  `python -m ozagia.integrations.kiro uninstall` removes them. The hooks
  refuse ref-moving git commands (exit 2), answer reads of node keys
  (`sym:`, `json:`, ...) from the graph, refresh the graph after edits and
  shell commands, and print a brief at session start. kiro-cli 2.37 does
  not pass PostToolUse output to the model. See docs/kiro.md;
  `scripts/kiro_probe_hook.py` records what a live session sends.
- `hookcore.run_hook` also takes plain-text answers and `HookBlock` (stderr
  and exit 2); `hookcore.node_key` takes the key prefixes to recognise
  (`STRUCTURED_KEY_PREFIXES` for `json:`/`yaml:`/`toml:`). Claude Code and
  Codex hooks are unchanged.

- Codex integration: `ozagia install codex [--scope project|user]` adds
  command hooks to `.codex/hooks.json` (git ref refusal before `Bash`, graph
  context after `Bash` and `apply_patch`, a short brief at `SessionStart`),
  the ozagia MCP server as `[mcp_servers.ozagia]` in `.codex/config.toml`
  (only that table is touched), and a marked section in `AGENTS.md`;
  `python -m ozagia.integrations.codex uninstall` removes all three. Codex
  runs the hooks after you trust them in `/hooks`. See docs/codex.md.
- `ozagia daemon ensure` starts the graph daemon detached when none answers,
  with an idle timeout (`serve --idle-timeout S`, 30 minutes when started by
  `ensure`). The Codex hooks call it on demand, and only for a git work
  tree, `$OZAGIA_ROOT` or a root with a graph snapshot. `serve` takes the
  daemon lock itself and exits at once when another daemon holds it.
- The generic parts of the Claude Code hook moved to
  `ozagia.integrations.hookcore`.
- Daemon stats count requests by harness (`by_harness`).

## 0.1.2 (2026-11-16)

First published release. (`v0.1.0` and `v0.1.1` were tagged, but their release
builds failed and they were never published.)

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
