# ozagia reference

> **Generated file.** Built by `scripts/docs/generate_docs.py` from `pyproject.toml`, `src/ozagia/` (ozagia `0.1.5.dev0`). Do not edit by hand: change the source code and regenerate.

Generated from the code by `scripts/docs/generate_docs.py`: the command inventory from `[project.scripts]`, the command pages from each module's argparse tree, the MCP pages from the server's tool lists, and the configuration page from the settings registry. Regenerate after changing any of them.

## Commands (40)

| command | what it does | reference |
|---|---|---|
| `ozagia` | The ozagia command: the graph, the daemon, the integrations and the agent. | [cli/ozagia.md](cli/ozagia.md) |
| `ozagia-memory` | ozagia.core.memory — a mechanical repo-memory index and query layer. | [cli/ozagia-memory.md](cli/ozagia-memory.md) |
| `ozagia-view` | render a graph snapshot through a data-defined aperture | [cli/ozagia-view.md](cli/ozagia-view.md) |
| `ozagia-status` | read-only workspace, resource, file, runtime, daemon and snapshot status | [cli/ozagia-status.md](cli/ozagia-status.md) |
| `ozagia-session-profile` | Read-only, metadata-only aggregation of daemon session activity. | [cli/ozagia-session-profile.md](cli/ozagia-session-profile.md) |
| `ozagia-settings` | inspect the typed settings registry and preview writes | [cli/ozagia-settings.md](cli/ozagia-settings.md) |
| `ozagia-tasks` | validate, preview, run and report bounded tasks | [cli/ozagia-tasks.md](cli/ozagia-tasks.md) |
| `ozagia-task-library` | browse, validate and copy built-in task classes | [cli/ozagia-task-library.md](cli/ozagia-task-library.md) |
| `ozagia-research-ui` | render a recorded research investigation as a read-only JSON or HTML view | [cli/ozagia-research-ui.md](cli/ozagia-research-ui.md) |
| `ozagia-log-inspect` | bounded, redacted inspection of explicitly listed local logs (OpenCode, daemon journal, eval results, JSONL/text) | [cli/ozagia-log-inspect.md](cli/ozagia-log-inspect.md) |
| `ozagia-models` | inspect available models and resolve a task's model | [cli/ozagia-models.md](cli/ozagia-models.md) |
| `ozagia-testing` | inspect the test policy and plan affected tests | [cli/ozagia-testing.md](cli/ozagia-testing.md) |
| `ozagia-actions` | Command line for preview-first process and connection actions. | [cli/ozagia-actions.md](cli/ozagia-actions.md) |
| `ozagia-repo-scan` | advisory, read-only repository scan | [cli/ozagia-repo-scan.md](cli/ozagia-repo-scan.md) |
| `ozagia-upstream` | consent-based, redacted local field-improvement bundles; nothing is uploaded | [cli/ozagia-upstream.md](cli/ozagia-upstream.md) |
| `ozagia-module-build` | preview-first module scaffolding from a repo-scan report | [cli/ozagia-module-build.md](cli/ozagia-module-build.md) |
| `ozagia-ocelot-code` | optional Ocelot structural clustering for code findings; Ocelot is never installed or downloaded | [cli/ozagia-ocelot-code.md](cli/ozagia-ocelot-code.md) |
| `ozagia-hooks` | opt-in Git hooks for the worktree commit/push policy | [cli/ozagia-hooks.md](cli/ozagia-hooks.md) |
| `ozagia-console` | serve a loopback-only, read-only ozagia console | [cli/ozagia-console.md](cli/ozagia-console.md) |
| `ozagia-security` | advisory, offline security and antipattern scan | [cli/ozagia-security.md](cli/ozagia-security.md) |
| `ozagia-secrets` | secret references, provider status and redacted checks | [cli/ozagia-secrets.md](cli/ozagia-secrets.md) |
| `ozagia-library-hints` | advisory library-first hints for a diff or changed list | [cli/ozagia-library-hints.md](cli/ozagia-library-hints.md) |
| `ozagia-toolchain` | external-tool discovery, capability readiness and install previews (never installs) | [cli/ozagia-toolchain.md](cli/ozagia-toolchain.md) |
| `ozagia-languages` | per-language support packages, detection and installs | [cli/ozagia-languages.md](cli/ozagia-languages.md) |
| `ozagia-supply-chain` | offline supply-chain attack detection (never fetches, installs or executes project code) | [cli/ozagia-supply-chain.md](cli/ozagia-supply-chain.md) |
| `ozagia-review` | deterministic, graph-aware review of a BASE..HEAD diff | [cli/ozagia-review.md](cli/ozagia-review.md) |
| `ozagia-ocelot` | resolve, install and verify a separate Ocelot checkout; Ocelot is not bundled and is never downloaded implicitly | [cli/ozagia-ocelot.md](cli/ozagia-ocelot.md) |
| `ozagia-ocelot-workspace` | query an indexed Ocelot checkout: seed impact, a scoped test plan, the Rust/Python/seed bridge, research links, seed validation, and a read-only engine/worktree doctor | [cli/ozagia-ocelot-workspace.md](cli/ozagia-ocelot-workspace.md) |
| `ozagia-worktree` | read-only worktree, branch, drift and policy checks | [cli/ozagia-worktree.md](cli/ozagia-worktree.md) |
| `ozagia-resources` | read-only host resources, history and admission checks | [cli/ozagia-resources.md](cli/ozagia-resources.md) |
| `ozagia-files` | track created files and route Markdown without deleting | [cli/ozagia-files.md](cli/ozagia-files.md) |
| `ozagia-mcp` | ozagia.integrations.mcp.server — stdio MCP server over the dev-memory snapshot. | [cli/ozagia-mcp.md](cli/ozagia-mcp.md) |
| `ozagia-research` | Integration layer for research graph, source lookup and mechanical checks. | [cli/ozagia-research.md](cli/ozagia-research.md) |
| `ozagia-edit` | ozagia.edit.code_edit — graph-addressed structural edits for Python source. | [cli/ozagia-edit.md](cli/ozagia-edit.md) |
| `ozagia-symbols` | ozagia.core.symbol_graph — symbol-level callers, callees and impact. | [cli/ozagia-symbols.md](cli/ozagia-symbols.md) |
| `ozagia-eval` | ozagia.eval.agent — end-to-end task-success harness for coding agents. | [cli/ozagia-eval.md](cli/ozagia-eval.md) |
| `ozagia-eval-retrieval` | ozagia.eval.retrieval — deterministic, offline retrieval evaluation. | [cli/ozagia-eval-retrieval.md](cli/ozagia-eval-retrieval.md) |
| `ozagia-eval-benchmarks` | Hugging Face benchmark ingestion and retrieval verification. | [cli/ozagia-eval-benchmarks.md](cli/ozagia-eval-benchmarks.md) |
| `ozagia-bench` | ozagia.eval.bench — timing bench for the warm repo index. | [cli/ozagia-bench.md](cli/ozagia-bench.md) |
| `ozagia-profile` | Join profiler/telemetry artifacts to the repository graph | [cli/ozagia-profile.md](cli/ozagia-profile.md) |

## MCP tools

| surface | tools | reference |
|---|---|---|
| coding (default) | 98 | [mcp/coding.md](mcp/coding.md) |
| research | 107 | [mcp/research.md](mcp/research.md) |
| combined | 204 | [mcp/combined.md](mcp/combined.md) |
| graph (`--surface graph`) | 12 | [mcp/graph.md](mcp/graph.md) |

## Configuration

[config.md](config.md) — the settings registry and the `.ozagia.toml` model.
