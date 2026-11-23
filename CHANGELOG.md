# Changelog

## Unreleased

- Rust parity (Phases 1–7): tree-sitter structural tags, symbols, modules and
  imports; symbol-addressed Rust edits with a parse gate; rust-analyzer LSP
  parity with workspace-root routing and install hints; captured `cargo test`
  with stable human-output parsing, target-level affected selection and cache;
  a validated Python→Rust migration overlay with inventories, matcher, hints,
  status/tasks/scaffold and `dev_migration`; and the optional SCIP fixture,
  benchmark and Rust tier declaration. The tier remains syntactic by default;
  rust-analyzer/SCIP are optional precise layers, and a Rust dataflow analyzer
  is a separate plan.
- `cargo test` capture supports package/target/feature/filter mapping, reports
  linked targets after a change, skips cached targets, never caches failures,
  handles `test = false`/`harness = false`, and records captured failures in
  the daemon working set and root test record without clearing the other test
  runner's failures.
- `ozagia migration status|tasks|scaffold` and `dev_migration` read bounded
  Python and Rust inventories, keep explicit pairs authoritative, report
  inference candidates without accepting them, and keep scaffold preview-only
  unless the CLI `--write` transaction is used.

- Repo map retrieval now scores exact definition names and path segments above
  substring hits, demotes tests/docs/vendored files, and fuses the graph/name
  ranking with bounded BM25 content candidates using confidence-weighted rank
  fusion instead of flat reciprocal-rank fusion. It caps the task header,
  drops candidates below 18% of the top fused score (at most 32 files), and
  spends the same token budget on one full body, compact outlines,
  matched-line snippets for lexical hits, and cheap listings. A per-file
  confidence score now gates low-confidence files: below 35% of the top score
  at small budgets (30% above), a file must add task evidence not already
  covered. The evaluator also reports line-IDF-weighted evidence coverage and
  density (unique task terms plus matched definitions per 1000 tokens). On the
  23-task SWE-bench Verified stratified baseline, `dev_repo_map` reached
  0.935/0.967/0.967 file recall, 0.899/0.957/0.967 content recall,
  0.087/0.068/0.059 precision, 0.193/0.338/0.387 evidence coverage and
  90.07/104.18/104.05 evidence per 1k tokens at 500/900/1500, with all runs
  within budget. A 10-task Django holdout scored 0.900/0.900/0.900 recall and
  0.700/0.900/0.900 content recall. `min_confidence=0` restores the
  recall-first gate; `content_search=False` preserves the graph-only map.
- Hugging Face benchmark ingestion (`ozagia-eval-benchmarks`): explicit
  `--allow-network` fetch/import of tagged datasets, cached dataset revisions,
  SWE-bench Verified/Lite gold-file task conversion, safe GitHub archive
  checkouts, and per-repo retrieval verification. File localization only; no
  target tests, target code or model calls. Licence metadata is recorded as
  unknown when the dataset card does not state one.
- Disk admission is work-aware and absolute. The default free-space check uses
  `resources.min_free_disk_gb`, `resources.reserve_disk_gb` and the requested or
  historical p90 disk size; `reserve_disk_pct` now defaults to 0 and
  `min_free_disk_pct` is removed. A large filesystem with a low percentage free
  is no longer refused when the requested work fits. A host-floor refusal now
  reports the actual GiB shortfall.
- `ozagia profile` (and `ozagia-profile`) reads existing cProfile/pstats,
  Speedscope/py-spy, Chrome/Node CPU, folded/collapsed stack, OTLP JSON trace
  and metric, and Prometheus text artifacts. It normalizes durations, sample
  counts, metric values and histogram buckets, maps active frames to tag-cache
  definitions, adds syntactic callers/impact from the symbol graph, and reports
  hotspots, paths, files/modules, telemetry operations, metrics and optional
  baseline regressions. `ozagia profile run --allow-exec` adds opt-in guided
  collection for cProfile, py-spy and Node under resource admission and
  rlimits. Artifact ingestion never starts a profiler, imports the target,
  calls a model or uses the network; `--out` is the only repository write (a
  missing tag cache may be built under the state directory).
- `ozagia deep-review` writes an offline-by-default, subject-based report
  directory: `README.md`, `00-method-and-scope.md`, eleven subject files,
  `findings.json`, `findings-index.md` and `manifest.json`. It composes
  repository review, security and static Python checks, dependency and
  operations inventories, documentation checks, optional `--range` change
  review and baseline comparison; the manifest records coverage, budgets,
  truncation, graph freshness, evidence hashes and output hashes, with
  `exhaustive` and `truth_assessed` false. `--fail-on` is `never` by default
  (exit 0 for a report, 1 for a gate match, 2 for invalid or refused input).
  The optional `--model-pass` requires `--live` and `OZAGIA_EVAL_LIVE=1` and
  writes advisory, unverified candidates to `model-review.json` without
  changing findings or exit status.
- Deep review additions: a `--profile change`/`review-change` compact preset
  (full review plus `HEAD~1..HEAD`, default max 20 findings, `--fail-on never`
  unless explicit); explicit `--security-tool`/`--security-network` with
  requested-tool and per-tool status/version/error/finding/report-hash
  provenance; bounded `evidence/` excerpts and `sbom.json` with CVE state
  unknown; optional exact-version matching or introduced/fixed OSV-style
  SemVer range matching against an explicit local advisory file (ranges support
  Node/Rust and `v`-prefixed Go, while Python remains exact-version only);
  graph content hash and per-subject report hashes; exact-ID baseline
  comparison with `identity_policy`, `move_policy`, `move_mapping: unknown` and
  subject/severity deltas; a read-only `ozagia-status` reviews section; and
  model-pass time/request/token/cost budgets, role mapping and opt-in Ocelot
  candidate clustering. Model candidates remain advisory and unverified.
- `ozagia deep-review --profile-report PATH` consumes measured profile reports
  and artifacts. Hotspots and baseline regressions become performance findings
  with observed evidence; telemetry error spans become failure-mode findings;
  metric inventory is attached to the performance/scalability manifest.
- `ozagia agent` reports its launch while the terminal would otherwise be
  blank. The interactive TUI no longer waits for the graph: the snapshot
  build/refresh and the daemon start run in the background and each phase
  goes to `logs/launch_status.json`, which the TUI sidebar shows
  (`OzAGIa G:building D:waiting 4s` → `G:fresh D:ready`). A session that ends
  first cancels the preparation. A non-interactive run still waits and reports
  the synchronous phases on stderr with a ticking status line (and a build
  says why it rebuilt). The TUI brand is centred under the home-screen logo
  instead of starting at the logo's left edge.

- Model metadata sources (`ozagia.core.model_sources`, docs/model_sources.md):
  an explicit source registry and cache for models.dev, LiteLLM, OpenRouter
  and Epoch AI, with `sources`, `sync` and `import` commands. `sync` refuses
  the network without `--allow-network`; caches live under
  `<state>/models/` with SHA-256 metadata and are read by `graph`, `similar`,
  `match` and `show` by default (`--source`, `--no-sources`). Merge precedence
  is models.dev, LiteLLM, OpenRouter, Epoch; an explicit model override still
  wins. Epoch supplies parameter counts only for conservative name matches.
  The graph accepts the external `profile_source` field. No automatic sync, no
  credentials and no model calls.
- W23 fix round. Lock-file tools (`pdm info --python`, `poetry env info
  --path`) run only for a trusted project and never in `read` cache mode;
  `.pdm-python` is read as text and its target judged project-shipped like
  any candidate, and `review-repo` never runs the tools. `validate` resolves
  `--base` to a commit (`base_commit`) and records the check interpreter's
  state (`check_python_state`: signature and required-module presence);
  `--validation` reuse refuses a moved base or a changed interpreter. The
  timing headline and its deltas cover the (task, repeat) cells valid in
  every arm (`paired_timing`), labelled, with each arm's valid runs and
  every run beside them. Without validation, an environment failure on a
  module no file at base imports (and the break patch does not add) counts
  against the agent. `report` treats a missing check exit code with a
  check log as a timeout. `module_environment`'s memo is checked against
  interpreter, site-packages and manifest mtimes, and `project_roots` is
  cached per root.
- Environments, failure classes and timing integrated (W23). The agent
  eval has one validation: `run`'s preflight by default, a reused `validate
  --out-json` report with `--validation` (same base, check interpreter and
  task fingerprints, else exit 2), or none with `--skip-validate`; the same
  report decides validity (`validated_tasks`), and without one the import
  probe does. `failures` takes W20's `{interpreter, version}` items for
  `available_in` and adds `declared_in` (the manifests that declare the
  missing module, `[]` for none) through
  `environments.module_environment`, memoized per root and module with the
  user's trust rule. The report header gives the check interpreter, the
  validation, success over valid runs with the paired rate, then timing;
  timing covers valid runs only, like success, with per-task time over every
  run beside it. `report` classifies runs recorded before failure classes
  from their check logs (`--repo` for first-party names). The harness no
  longer writes probe or import caches into `--repo` when it picks the check
  interpreter.
- Python environments (`ozagia.core.environments`, docs/environments.md):
  `ozagia env [--for PATH ...] [--json] [--no-cache]` and the MCP tool
  `dev_environment` list a project's interpreters ($VIRTUAL_ENV, root and
  source-repo venvs, lock-file tool envs, command docs, PATH), probe each with
  `python -I -c` (bounded, cached under the state dir by interpreter path,
  `pyvenv.cfg` and site-packages mtimes) and rank them for given files by their third-party
  imports, including the transitive first-party closure and conftests of
  tests. `who_has(root, module)` names the interpreters that have a module.
  The manifest readers moved to `ozagia.core.manifests` and now say which
  declarations are optional. The repo review's dependencies section adds
  `undeclared_import`, `optional_only_import` and `test_env_gap`. The eval
  harness defaults `--python` to the interpreter `env_for` picks for the
  checks' test files in `--repo` and records it per run (`check_python`,
  `check_python_reason`); `run` validates the selected tasks first and
  refuses with exit 2 unless `--skip-validate`, and the report records
  `validated` and a `validation` summary.
  Interpreters the project ships (root or source-repo venvs, in-root doc
  paths, a lock tool's answer inside) are run only with
  `OZAGIA_TRUST_PROJECT_PYTHON=1` or user-level `[environments]
  trust_project_python = true`; otherwise they are read statically from
  `pyvenv.cfg` and site-packages. Probes run with a minimal environment.
  Imports after a function-level `pytest.importorskip` are guarded; imports
  under version/platform tests are `conditional` and evaluated per
  interpreter where possible. Parsed imports are cached by git blob id, and
  `--compare` treats the probed interpreter set as part of the import
  checks' population.
- `config._SENSITIVE_KEY` matches option names and `key=value` arguments,
  not substrings of paths (`tests/test_tokenizer.py` and a checkout under
  `…/environments/` are accepted in a command argv).

- Deterministic failure classes (`ozagia.core.failures`): `harness`,
  `timeout`, `environment`, `collection`, `assertion`, `error`, `passed`
  (and `unknown`), from parsed pytest output (failure and collection
  blocks, the short summary, startup errors, the daemon's own lines;
  captured output and assertion diffs are skipped), with a class per
  failing item; a run is `environment` only when every failing item is.
  The agent eval classifies each check and the agent's last pytest in the
  transcript; a `harness` or `environment` check makes a run invalid only
  when the agent cannot have caused it (validated base, else an import
  probe outside the worktree; `validity_basis`), invalid runs leave
  success counts and rates, and `paired_rate` compares arms over the cells
  valid in every arm. A captured pytest that did not pass gets an
  `[ozagia] failure class` line and `failure item` lines for tests with
  an environment cause; the test record keeps them (captured runs only),
  and goal test criteria give `unknown` per test for environment causes
  and for harness runs.
- Ocelot test-impact and architecture-pattern candidates:
  `ocelot_code.change_shape` records changed files, graph symbols, edge
  types, test links, import/package directories and a stable digest;
  `test_impact_clusters` groups similar shapes into candidate test sets
  (exact digest/overlap, optional Ocelot analogy, injected clusterer) and
  `architecture_patterns` groups scan opportunities or graph file shapes into
  candidate patterns. Both report `truth_assessed: false`, never claim
  completeness, and never execute tests. Code review adds opt-in
  `ocelot.impact`/`ocelot.patterns` sections (`--ocelot-impact`,
  `--ocelot-patterns`), repo scan adds opt-in `ocelot.patterns`
  (`--ocelot-patterns`), and `ozagia.ocelot_code_cli` gains `impact` and
  `patterns` commands. Missing or failing Ocelot is reported with an exact
  fallback; no install, network or model call.

- Config migration and opt-in Git hooks: `ozagia config migrate [--root R]
  [--dry-run|--apply --confirm TOKEN]` copies the supported `.ozagia.toml`
  sections (index, memory, adapters, reads, tests, jobs, lint, security,
  hints, review layers, IaC roots) into the typed registry at
  `ozagia/config.toml`, with a unified diff, an apply token, conflict and
  unknown/unmapped reporting, one atomic write and no deletion of the legacy
  file. `ozagia hooks install|uninstall|status --pre-commit|--pre-push`
  writes or removes small executable policy wrappers (configured Python,
  redacted failure output, no silent install), preserves foreign hooks
  unless `--force`, and is idempotent. Registry additions cover the legacy
  index/read/lint/jobs/security groups and `testing.cache`.

- Repo review and goals together (W19): `review_resolved` reads the newest
  packet in `.ozagia/reviews/` (`latest.json`, else the newest timestamped
  one) and decides with `repo_review.finding_state`, using the finding's
  `type` and `keys` stored at creation (present -> fail, not_checked ->
  unknown). It passes only on a packet written by `review_repo` (new
  `generator` field) for the same root, with a later `generated_at` (now
  with microseconds) and another sha256 than the packet the goal was made
  from, and is `stale` when a target, scope or finding file changed since
  that review started (new packet field `tree`, the working-tree
  fingerprint test runs use; mtime fallback for packets without it).
  `goal_add` with `review_resolved` needs a packet that contains the
  finding. `ops:` keys resolve from the tracked operations sources when
  the graph has no ops nodes (`repo_review.declared_ops_nodes`, sharing
  `ops_graph.iter_ops_sources` with the ops section), `file:` keys also
  resolve to a regular file in the working tree, and `ops_has` takes
  `pinned: true` (`ops_graph.image_pinned`, shared with `unpinned_image`;
  a build stage's goal now names `base_image`). `ozagia goal from-review
  [PACKET] FINDING_ID` defaults to `latest.json`; MCP `goal_from_review
  {finding_id}` is new, and `dev_review_repo` names the goal tracking each
  top finding. Fixed: the freshness files of `ops:` targets (the source
  path, not `compose/<path>`).

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
  (`working_set/tests.json`) now also keeps the last 50 runs (start time,
  selectors, failed and errored keys, completeness, source, and for a
  pytest the daemon ran itself a working-tree fingerprint), so a test's
  last pass has a time. Test criteria take only daemon-captured runs as
  evidence and are `stale` when a target, scope or test file's content
  changed since the run started (content hashes, not mtimes). Review
  packets must be regular files under `.ozagia/reviews/` of at most 16 MB.
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
