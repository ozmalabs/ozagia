# MCP tools — combined surface

> **Generated file.** Built by `scripts/docs/generate_docs.py` from `src/ozagia/integrations/mcp/server.py`, `src/ozagia/integrations/mcp/graph_surface.py`, `src/ozagia/integrations/mcp/sre.py`, `src/ozagia/research/mcp.py` (ozagia `0.1.5.dev0`). Do not edit by hand: change the source code and regenerate.

195 tools.

> The coding and research sets together, plus the `research_dev_context` bridge tool.

> Conditionally listed: `dev_rule` (only when the snapshot has Rule nodes), `dev_seed` (only when the snapshot has Seed nodes).

### `dev_architecture`

Deterministic architecture summary: hubs, reference cycles (Tarjan SCC), coupling, module boundaries, dead-code candidates (heuristic) and test/source ratio. File-level and syntactic; use LSP/SCIP for compiler-accurate answers.

| parameter | type | required | default | description |
|---|---|---|---|---|
| `top` | integer |  | 15 | — |
### `dev_ast`

The Python AST graph for a file: structural paths (e.g. body[3].body[0].value), node types, line spans and previews. Use these paths as `target` for generic replace_node / insert_before_node / insert_after_node / delete_node / wrap_node ops.

| parameter | type | required | default | description |
|---|---|---|---|---|
| `file` | string | yes |  | Repo-relative .py file. |
| `node` | string |  |  | Optional AST path to show as a subtree. |
| `max_nodes` | integer |  | 200 | — |
### `dev_aws_cli_check`

Check an AWS CLI service/operation against local models; no network.

| parameter | type | required | default | description |
|---|---|---|---|---|
| `argv` | array | yes |  | — |
### `dev_brief`

Pre-load a compact repo brief: layout, build/test commands, key docs, recent commits, working-tree changes, known failures, and (optionally) a task context pack. Call it once at session start or when you need cheap orientation without reading many files.

| parameter | type | required | default | description |
|---|---|---|---|---|
| `task` | string |  |  | Optional task to append as a context pack. |
| `budget_tokens` | integer |  | 4000 | — |
| `write` | boolean |  | false | Write .ozagia/brief.md too. |
### `dev_callees`

Outgoing calls (callees) at a symbol position via LSP. Positions are 0-based LSP (line and character); dev_symbol and dev_file report 1-based lines, so subtract 1.

| parameter | type | required | default | description |
|---|---|---|---|---|
| `file` | string | yes |  | — |
| `line` | integer | yes |  | 0-based line (LSP-native). |
| `character` | integer | yes |  | 0-based UTF-16 character (LSP-native). |
| `language` | string |  |  | — |
### `dev_callers`

Incoming calls (callers) at a symbol position via LSP. Positions are 0-based LSP (line and character); dev_symbol and dev_file report 1-based lines, so subtract 1.

| parameter | type | required | default | description |
|---|---|---|---|---|
| `file` | string | yes |  | — |
| `line` | integer | yes |  | 0-based line (LSP-native). |
| `character` | integer | yes |  | 0-based UTF-16 character (LSP-native). |
| `language` | string |  |  | — |
### `dev_capabilities`

Read-only catalogue of Ozagia's declared capabilities and a coverage audit against the observed CLI, MCP, daemon, toolchain and status surfaces. action=list pages capabilities, action=show returns one capability with its nodes/edges, action=audit reports the two mismatch directions (declared_surface_not_observed and observed_surface_undeclared) plus explicit coverage and readiness limits and the current unknown findings. Bounded by limit/cursor. It never probes, starts a daemon, refreshes a graph, imports target code or calls a model.

| parameter | type | required | default | description |
|---|---|---|---|---|
| `action` | string |  | "list" | list capabilities, show one, or audit. |
| `id` | string |  |  | capability id for action=show, e.g. cap:graph.context. |
| `limit` | integer |  | 25 | page size (default 25, max 200). |
| `cursor` | string |  |  | opaque next cursor from a previous page. |
### `dev_changed`

Working-tree changes from `git status --porcelain`, with graph links for changed files. Use it to see what the current session has touched, whether the snapshot is stale, and whether `dev_refresh` has files to reingest.

| parameter | type | required | default | description |
|---|---|---|---|---|
| `all` | boolean |  | false | Include generated/eval/corpus paths. |
| `limit` | integer |  | 200 | — |
### `dev_check_iac`

Run bounded, source-only Terraform/OpenTofu checks; no plan or providers.

| parameter | type | required | default | description |
|---|---|---|---|---|
| `root` | string |  |  | — |
### `dev_code_search`

Lexical hybrid code search: BM25 over paths/symbols/source lines, reranked by repo-map PageRank via reciprocal rank fusion. Use it to locate candidates, then dev_context/dev_repo_map to expand and LSP/SCIP for precise navigation.

| parameter | type | required | default | description |
|---|---|---|---|---|
| `query` | string | yes |  | — |
| `limit` | integer |  | 20 | — |
| `include_snippet` | boolean |  | true | — |
### `dev_context`

Build a cited context pack for a development task. Use this FIRST when starting a task in this repo: it returns the relevant seed / rule / file / symbol / test / doc nodes with file paths and line ranges, so you can read the actual source rather than guess. The pack is selected mechanically from the repo graph; it is not a decision about what to change.

| parameter | type | required | default | description |
|---|---|---|---|---|
| `task` | string | yes |  | Natural-language task or question. |
| `max_items` | integer |  | 8 | Maximum context items (default 8). |
| `mode` | string |  | "auto" | auto: full top anchors, outlines for the rest. |
| `budget_tokens` | integer |  | 6000 | Estimated token budget. |
### `dev_deep_review`

Compose a bounded offline subject-based review report. Writes only below .ozagia/reviews; does not run tests or models.

| parameter | type | required | default | description |
|---|---|---|---|---|
| `profile` | string |  | "full" | full, security, performance, failure-modes, data-structures, scalability, architecture, tests, dependencies, operations, or docs. |
| `budget_s` | number |  | 30 | Collection budget in seconds (maximum 300). |
| `max_findings` | integer |  | 20 | Maximum findings in the returned summary (maximum 50). |
| `profile_reports` | array |  |  | Measured profile reports/artifacts under the workspace. |
### `dev_definition`

Compiler-accurate definition at a position via LSP. Positions are 0-based LSP (line and character); dev_symbol and dev_file report 1-based lines, so subtract 1.

| parameter | type | required | default | description |
|---|---|---|---|---|
| `file` | string | yes |  | — |
| `line` | integer | yes |  | 0-based line (LSP-native). |
| `character` | integer | yes |  | 0-based UTF-16 character (LSP-native). |
| `language` | string |  |  | — |
### `dev_diagnostics`

LSP push diagnostics for a file (published issues only).

| parameter | type | required | default | description |
|---|---|---|---|---|
| `file` | string | yes |  | — |
| `language` | string |  |  | — |
### `dev_document_symbols`

LSP document symbol hierarchy for a file.

| parameter | type | required | default | description |
|---|---|---|---|---|
| `file` | string | yes |  | — |
| `language` | string |  |  | — |
### `dev_edit`

Apply a GRAPH-ADDRESSED edit script. The ops are semantic, not a text diff: Python (replace_symbol, replace_body, insert_import, replace_node, insert_before_node, insert_after_node, delete_node, wrap_node, ...), Markdown (section ops), JSON (path ops). Preview by default; set apply=true to write atomically (with a backup and journal entry). A raw diff/patch is refused; `diff_view` is output only.

| parameter | type | required | default | description |
|---|---|---|---|---|
| `file` | string | yes |  | Repo-relative .py, .md, .json, .rs or .lua path. |
| `ops` | array | yes |  | Ordered semantic operations. |
| `apply` | boolean |  | false | — |
| `base_sha256` | string |  |  | Optional optimistic-concurrency check. |
| `cst_graph` | object |  |  | Optional synthetic/post-parsed CstNode graph for .rs/.lua when tree-sitter is unavailable. |
| `task` | string |  |  | Optional journal task label. |
| `diagnostics` | boolean |  | false | set diagnostics=true to wait up to 8 s for language-server diagnostics on the edited file. Only after an applied write; skipped when no server is available. Ignored in preview. |
### `dev_environment`

Which Python interpreter can run what in this project. Without arguments: the interpreters found ($VIRTUAL_ENV, .venv/venv/.venv-*, the source repository's venv for a worktree or clone, lock-file tool envs, interpreters named in command docs, python/python3 on PATH) with version, prefix and distribution count, and the declared Python dependencies. With `paths`: the interpreters under which every third-party import of those files (and of the first-party modules they import) resolves, best first, and per import which interpreters have it and which manifest declares it. With `module`: the interpreters that can import it. Use it before running tests with a `python` you have not checked. Interpreters the project ships (venvs in the root or the source repository) are not run unless the user trusts them; they are read from pyvenv.cfg and site-packages instead (`probe: static`).

| parameter | type | required | default | description |
|---|---|---|---|---|
| `paths` | array |  |  | Root-relative files or directories, e.g. tests/test_x.py. |
| `module` | string |  |  | A module name, e.g. numpy: which interpreters have it. |
### `dev_environments`

Declarative environment profiles; never a live account or cluster read.

Input schema: `{"type": "object", "properties": {}, "required": [], "additionalProperties": false}`

### `dev_file`

Everything the graph knows about one file: symbols, tests, imports/imported_by, docs, known failures and recent commits.

| parameter | type | required | default | description |
|---|---|---|---|---|
| `path` | string | yes |  | Repo-relative path, e.g. domains/teach.py. |
### `dev_files_explain`

Explain one file's ledger record: its creator (session, harness, task, tool), reason, classification, references, cleanup policy, whether it changed since it was recorded, and for Markdown its routing suggestion (ozagia.core.files.markdown_route).

| parameter | type | required | default | description |
|---|---|---|---|---|
| `path` | string | yes |  | Repo-relative or absolute file path. |
### `dev_files_list`

List the created-file ledger: files ozagia attributed to a session (handled writes, edits and after-call scans, plus session start/end scans) with path, hash, size, session, harness, tool, reason, classification and cleanup policy. No file contents or prompts are stored. Deduplicated by path and hash.

| parameter | type | required | default | description |
|---|---|---|---|---|
| `classification` | string |  |  | Only this classification (docs, plan, result, note, history, scratch, source, test, ...). |
| `status` | string |  | "active" | Only this status (default: active). |
| `limit` | integer |  | 100 | Most rows to return (at most 1000). |
### `dev_hover`

LSP hover/type/documentation at a position. Positions are 0-based LSP (line and character); dev_symbol and dev_file report 1-based lines, so subtract 1.

| parameter | type | required | default | description |
|---|---|---|---|---|
| `file` | string | yes |  | — |
| `line` | integer | yes |  | 0-based line (LSP-native). |
| `character` | integer | yes |  | 0-based UTF-16 character (LSP-native). |
| `language` | string |  |  | — |
### `dev_impact`

Transitive dependents of a file over the tree-sitter reference graph (who references symbols defined here). Use before changing a widely used module.

| parameter | type | required | default | description |
|---|---|---|---|---|
| `file` | string | yes |  | — |
| `depth` | integer |  | 2 | — |
| `scip_path` | string |  |  | — |
### `dev_key_edit`

Replace the WHOLE text of one node, named by its key: a symbol or test (sym:path::Name, test:path::test_x), a Markdown or TeX section (doc:path.md#section-slug as reads and outlines show it; #parent/child is accepted too), a line range (text:path#L10-L42) or a JSON value (json:path.json#/a/0). base_sha256 is the sha256 of the node's current text (node sha256 in region replies; the file's sha256 only when the node is the whole file); a missing, stale or file-only one is refused with the node's current text and its node sha256, so retry with that. Parse guard, backup, journal and graph update as dev_span_edit. Preview by default; apply=true writes.

| parameter | type | required | default | description |
|---|---|---|---|---|
| `key` | string | yes |  | Node key: sym:, test:, doc:<file>#<section>, text:<file>#L<a>-L<b>, json:<file>#<pointer>. |
| `new_text` | string | yes |  | The node's complete new text. |
| `base_sha256` | string |  |  | sha256 of the node's current text (of the file, for a whole-file node). |
| `apply` | boolean |  | false | — |
| `task` | string |  |  | Optional journal task label. |
### `dev_live_diff`

Diff two supplied live-state snapshot artifacts; no live read.

| parameter | type | required | default | description |
|---|---|---|---|---|
| `before` | string | yes |  | — |
| `after` | string | yes |  | — |
| `provenance` | string |  | "artifact" | — |
### `dev_live_import`

Validate a supplied live-state snapshot artifact; no live read.

| parameter | type | required | default | description |
|---|---|---|---|---|
| `snapshot` | string | yes |  | — |
| `provenance` | string | yes |  | — |
### `dev_live_save`

Validate and save a supplied snapshot under the state dir; no live read.

| parameter | type | required | default | description |
|---|---|---|---|---|
| `snapshot` | string | yes |  | — |
| `provenance` | string | yes |  | — |
### `dev_lsp_status`

LSP server availability for this checkout (does not start servers).

Input schema: `{"type": "object", "properties": {}, "required": [], "additionalProperties": false}`

### `dev_maps`

Read-only, bounded, versioned model-facing maps of this repository: graph, outline, capabilities or work/evidence. Use it to orient before reading or editing: map=graph for a key's bounded neighbourhood, map=outline for signatures/headings, map=capabilities for the declared capability audit including its directions and limits, and map=work to join a goal's recorded evidence plus the explicit omissions of that join. Each map carries schema/hash, source and snapshot identities, freshness, evidence and trust labels, omissions and truncation, plus configured/observed negotiation and explicit plain fallback. Bounded by limit/cursor. The tool only reads existing owners and writes bounded cache metadata under .ozagia/model_api/cache; it never refreshes the graph, starts a daemon, imports target code or calls a model.

| parameter | type | required | default | description |
|---|---|---|---|---|
| `map` | string |  | "graph" | map type to project. |
| `target` | string |  |  | graph key/path, outline target, or capability id. |
| `goal` | string |  |  | goal id for map=work, e.g. g1 or goal:g1. |
| `runs` | array |  |  | optional task run ids for map=work (max 8). |
| `tests` | array |  |  | optional test keys for map=work (max 16). |
| `format` | string |  | "structured" | structured v1 JSON or L0-style plain text. |
| `limit` | integer |  | 25 | page size (default 25, max 200). |
| `cursor` | string |  |  | opaque page cursor from a previous page. |
| `session` | string |  |  | host session label used for cache partition. |
| `model` | string |  |  | host model label used for cache partition. |
| `trace_id` | string |  |  | bounded redacted trace label. |
| `strict` | boolean |  | false | fail instead of using an explicit fallback. |
### `dev_migration`

Read-only Python-to-Rust migration overlay. view=status inventories the configured [migration] trees and reports per-status totals, top gaps/divergences, inference candidates (never accepted), freshness and explicit omissions; view=tasks emits a checklist for planned, in-progress or diverged symbols without creating a goal or running anything; view=scaffold renders a bounded skeleton preview for a simple dataclass/function. The MCP tool never writes: use `ozagia migration scaffold --write` for an explicit transaction.

| parameter | type | required | default | description |
|---|---|---|---|---|
| `view` | string |  | "status" | status, tasks or a scaffold preview. |
| `symbol` | string |  |  | public Python symbol key for view=scaffold. |
| `target` | string |  |  | existing .rs file inside the Rust tree for view=scaffold. |
| `limit` | integer |  | 25 | bounded list size (default 25, max 100). |
### `dev_neighbours`

Out- and in-neighbours of a node, grouped by edge type. Useful for dependency/seed/test/doc expansion.

| parameter | type | required | default | description |
|---|---|---|---|---|
| `id` | integer | yes |  | — |
| `edge` | string |  |  | Optional edge type filter. |
### `dev_node`

Show one graph node by integer id: type, attrs, source snippet and grouped neighbours. Ids come from dev_search/dev_context.

| parameter | type | required | default | description |
|---|---|---|---|---|
| `id` | integer | yes |  | — |
| `include_snippet` | boolean |  | true | — |
### `dev_ops_record`

Append a bounded, redacted ops receipt; no commands are replayed.

| parameter | type | required | default | description |
|---|---|---|---|---|
| `record` | object | yes |  | — |
### `dev_ops_resume`

Return a bounded, environment-scoped ops handoff summary.

| parameter | type | required | default | description |
|---|---|---|---|---|
| `limit` | integer |  | 20 | — |
| `environment` | string |  |  | — |
### `dev_outline`

Compact structural view of a file/symbol/seed/doc: signatures, docstring first lines, imports and test names, with line citations and no bodies. Use it to pre-load context cheaply; edit the real file, never this view.

| parameter | type | required | default | description |
|---|---|---|---|---|
| `target` | string | yes |  | File path, symbol, seed id, or doc path. |
### `dev_preflight_plan`

Build a dry-run, scrubbed emulator pre-flight plan; never executes it.

| parameter | type | required | default | description |
|---|---|---|---|---|
| `roots` | array | yes |  | — |
| `dependencies` | object |  |  | — |
| `emulator_bin` | string | yes |  | — |
### `dev_profile`

Read one existing profiler or OTLP JSON trace artifact and return bounded hotspots joined to graph symbols, callers and impact. It does not start a profiler, import the target, call a model or write a report. Use this to explain measured hot paths and performance regressions; a hotspot is evidence, not a causal verdict.

| parameter | type | required | default | description |
|---|---|---|---|---|
| `path` | string | yes |  | Repo-relative or absolute path to the artifact. |
| `format` | string |  | "auto" | — |
| `top` | integer |  | 10 | Hotspots to return (1..50). |
| `sort` | string |  | "self" | — |
| `interval_us` | integer |  |  | Microseconds per sample when the artifact uses counts. |
| `weight_unit` | string |  | "auto" | — |
| `no_graph` | boolean |  | false | Skip the tag-cache/symbol-graph join. |
### `dev_query`

Look inside a JSON, YAML or TOML file by JSON pointer (RFC 6901) instead of loading it in Python or jq: an object's keys with type and size, an array's length and a slice of its items, or a scalar's value, with its line (JSON) and node key (json:<path>#<pointer>). Output is capped at 8 kB. YAML needs the iac-yaml extra; TOML is read-only.

| parameter | type | required | default | description |
|---|---|---|---|---|
| `path` | string | yes |  | Repo-relative file, or a key such as json:results/run.json#/runs/0. |
| `pointer` | string |  |  | JSON pointer, e.g. /runs/0/score; empty or # is the root. |
| `format` | string |  |  | Default: from the file suffix. |
| `offset` | integer |  | 0 | 0-based index of the first array item or object key shown. |
| `limit` | integer |  |  | Items shown (default 20 array items, 50 object keys). |
### `dev_recent`

Recent dev-journal entries (decisions/verifications; built-in edits journaled for dev_who are left out).

| parameter | type | required | default | description |
|---|---|---|---|---|
| `n` | integer |  | 20 | — |
### `dev_record`

Append a decision or verification receipt to the shared, append-only journal of decisions and verifications. Use it after a scoped test/build command so the next agent/session can see what was tried and what passed.

| parameter | type | required | default | description |
|---|---|---|---|---|
| `kind` | string |  | "decision" | — |
| `task` | string | yes |  | — |
| `summary` | string | yes |  | — |
| `files` | array |  |  | — |
| `command` | string |  |  | — |
| `exit_code` | integer |  |  | — |
### `dev_references`

Compiler-accurate references at a position via LSP. Positions are 0-based LSP (line and character); dev_symbol and dev_file report 1-based lines, so subtract 1.

| parameter | type | required | default | description |
|---|---|---|---|---|
| `file` | string | yes |  | — |
| `line` | integer | yes |  | 0-based line (LSP-native). |
| `character` | integer | yes |  | 0-based UTF-16 character (LSP-native). |
| `language` | string |  |  | — |
### `dev_refresh`

Reingest files changed since the snapshot and return the graph delta (node keys added, removed, changed; edges).

Input schema: `{"type": "object", "properties": {}, "required": [], "additionalProperties": false}`

### `dev_relay_plan`

Build a dry-run relay command/tunnel plan; never executes or binds a live network.

| parameter | type | required | default | description |
|---|---|---|---|---|
| `spec` | object | yes |  | — |
| `argv` | array |  |  | — |
| `target` | string |  |  | — |
| `local_port` | integer |  | 0 | — |
### `dev_rename_preview`

LSP rename preview (review-only; never writes). Positions are 0-based LSP (line and character); dev_symbol and dev_file report 1-based lines, so subtract 1.

| parameter | type | required | default | description |
|---|---|---|---|---|
| `file` | string | yes |  | — |
| `line` | integer | yes |  | 0-based line (LSP-native). |
| `character` | integer | yes |  | 0-based UTF-16 character (LSP-native). |
| `language` | string |  |  | — |
| `new_name` | string | yes |  | — |
### `dev_replace`

Replace a pattern across files (use it instead of a script that rewrites files). Literal by default; regex=true takes a Python re pattern and a replacement with \1 / \g<name> groups. Scope by globs and/or node keys (sym:, doc:<file>#<section>, text:<file>#L<a>-L<b>: only matches inside those spans); by default only text files (known text suffixes, no dotfiles such as .env; any_suffix=true for every file). Preview by default: per-file match counts, enclosing keys, a short diff, `shas` and `plan_id`. Apply with apply=true and the preview's plan_id (required; base_shas alone is not enough): a plan that changed since the preview (the request, a file's bytes, the set of files), a match count other than `expect`, a cap, the scan's deadline, or a file the replace would stop parsing refuses the whole batch before any write. Each file is written with a backup; if a write fails, the files written before it are restored, and `partial` is set only when a restore fails. One journal entry; the graph is updated. .git, the state dir, symlinks, binary and oversized files are never touched; gitignore=false also scans files .gitignore excludes.

| parameter | type | required | default | description |
|---|---|---|---|---|
| `pattern` | string | yes |  | Literal text, or a regex with regex=true. |
| `replacement` | string | yes |  | Replacement (regex groups allowed with regex=true). |
| `regex` | boolean |  | false | — |
| `flags` | string |  |  | Regex flags as letters (i, m, s, x, a) or names (IGNORECASE,MULTILINE). |
| `globs` | array |  |  | Root-relative globs (** crosses dirs; a glob without / matches base names). |
| `keys` | array |  |  | Node keys whose spans restrict the matches. |
| `max_files` | integer |  |  | Refuse when more files match (default 200). |
| `max_matches` | integer |  |  | Refuse when there are more matches (default 5000). |
| `expect` | integer |  |  | Refuse unless the total match count is exactly this. |
| `apply` | boolean |  | false | Write the change; needs plan_id. |
| `base_shas` | object |  |  | Optional, with plan_id: the preview's `shas` (file -> sha256); apply refuses files changed since. |
| `plan_id` | string |  |  | The preview's plan_id (required to apply): apply refuses any difference from the preview. |
| `allow_parse_errors` | boolean |  | false | — |
| `gitignore` | boolean |  | true | false: also files .gitignore excludes. |
| `any_suffix` | boolean |  | false | true: every file, not only text suffixes; dotfiles included. |
| `task` | string |  |  | Optional journal task label. |
### `dev_repo_map`

Ranked, token-budgeted repo map (tree-sitter tags + personalized PageRank). Use it when you need the shape of the codebase rather than full snippets: full outlines for the hottest files, public symbols for the middle, a listing for the tail. Task-named seeds/files become focus files.

| parameter | type | required | default | description |
|---|---|---|---|---|
| `task` | string |  | "" | — |
| `budget_tokens` | integer |  | 1500 | — |
| `focus_files` | array |  |  | — |
| `force_refresh` | boolean |  | false | — |
| `scip_path` | string |  |  | Optional SCIP JSON export for precise edges. |
### `dev_review_repo`

Review the whole repository deterministically: architecture (import cycles, hubs, layering), tests (failing tests from the record, untested modules), health (large files and functions, TODO density, unreferenced symbols), churn hotspots without tests, stale doc references, security hygiene (tracked .env and key files, sensitive constructs; not a security audit), ops sources (unpinned images, restart policy, health checks, published ports) and dependencies (lock files; imports no manifest declares or only an optional one does; tests no found interpreter can import). Uses the graph only when it is fresh and says what it could not check. Returns counts and the top findings, each with an id, graph keys and a suggested goal; the full packet is written under .ozagia/reviews/ (latest.json is the newest).

| parameter | type | required | default | description |
|---|---|---|---|---|
| `sections` | array |  |  | Only these sections (architecture, tests, health, churn, docs, security, ops, dependencies); default all. |
| `compare_latest` | boolean |  | false | Compare finding ids with the previous latest.json packet. |
| `budget_s` | number |  | 30 | Wall-clock budget for the sections (at most 300 s). |
| `max_findings` | integer |  | 10 | Top findings to return (at most 50). |
### `dev_rule`

Find a seed rule by name and show its seed, file and rule body. Use this before editing a rule so you see the existing match / where / effects rather than re-deriving them.

| parameter | type | required | default | description |
|---|---|---|---|---|
| `name` | string | yes |  | — |
### `dev_runtime_logs`

Read one bounded local service/process log on request. Logs are never included in automatic context.

| parameter | type | required | default | description |
|---|---|---|---|---|
| `name` | string | yes |  | — |
| `max_bytes` | integer |  | 2048 | — |
### `dev_runtime_status`

Read configured services/tunnels and their observed local state. Never starts a process or opens a tunnel; a configured tunnel is not reported as connected.

| parameter | type | required | default | description |
|---|---|---|---|---|
| `name` | string |  |  | — |
### `dev_scip`

Import a SCIP JSON export (from `scip print --json`) and return normalized definitions/references, repo-map edges and per-file counts. Real .scip protobuf requires protobuf + scip_pb2; absent bindings are refused honestly.

| parameter | type | required | default | description |
|---|---|---|---|---|
| `path` | string | yes |  | — |
| `summary_only` | boolean |  | false | — |
### `dev_search`

Search the repo graph by name/path/title/description. Returns node ids, types and labels. Use dev_file/dev_symbol/dev_seed for details, or dev_node for attrs and edges.

| parameter | type | required | default | description |
|---|---|---|---|---|
| `text` | string | yes |  | — |
| `types` | array |  |  | Optional type filter, e.g. ['Seed','Rule']. |
| `limit` | integer |  | 20 | — |
### `dev_secrets`

Read-only secret status through the ozagia daemon: provider availability (providers), reference check results (check) and one session's in-memory capture metadata such as refs, fingerprints and expiry (list). It never returns a secret value, and this tool cannot capture or resolve one. A clean check means the provider is reachable, not that the credential is valid.

| parameter | type | required | default | description |
|---|---|---|---|---|
| `action` | string |  | "providers" | — |
| `refs` | array |  |  | References to check (action=check). |
| `session` | string |  |  | The harness's session id (needed for session references and list). |
| `harness` | string |  |  | opencode, claude, codex or kiro (default: opencode). |
### `dev_seed`

A seed's manifest: version, description, dependency closure, the rules/concepts/reflexes it ships, tests that mention it, and docs that document it.

| parameter | type | required | default | description |
|---|---|---|---|---|
| `seed_id` | string | yes |  | Seed id, e.g. gap_awareness. |
### `dev_session_finish`

Close a dev session with an outcome.

| parameter | type | required | default | description |
|---|---|---|---|---|
| `session_id` | string | yes |  | — |
| `outcome` | string | yes |  | — |
### `dev_session_list`

List persisted dev sessions.

Input schema: `{"type": "object", "properties": {}, "required": [], "additionalProperties": false}`

### `dev_session_next`

The next workflow action and its reason.

| parameter | type | required | default | description |
|---|---|---|---|---|
| `session_id` | string | yes |  | — |
### `dev_session_record`

Append one typed node to a dev session. Kinds: plan, step, attempt, edit, verification, decision. Returns the new node and the next workflow action.

| parameter | type | required | default | description |
|---|---|---|---|---|
| `session_id` | string | yes |  | — |
| `kind` | string | yes |  | — |
| `fields` | object |  |  | — |
### `dev_session_start`

Start a replayable dev-session graph (Task -> Plan -> Step -> Attempt -> Edit -> Verification -> Decision). Returns the new session id, state and the workflow's next action. This records facts only; it does not edit files or run tests.

| parameter | type | required | default | description |
|---|---|---|---|---|
| `task` | string | yes |  | — |
| `max_attempts` | integer |  | 3 | — |
### `dev_session_state`

The session graph snapshot plus the next action.

| parameter | type | required | default | description |
|---|---|---|---|---|
| `session_id` | string | yes |  | — |
### `dev_span_edit`

Replace an exact text span in a file. The span is located in the graph (the enclosing symbol/section/chunk key is returned), the edit is refused if it breaks the file's parse, applied atomically with a backup when apply=true, the graph is updated, and the result lists the graph delta and broken references (missing imported names, call arity mismatches). Preview by default.

| parameter | type | required | default | description |
|---|---|---|---|---|
| `file` | string | yes |  | Repo-relative path. |
| `old_string` | string | yes |  | Exact text to replace; must be unique unless replace_all=true. |
| `new_string` | string | yes |  | Replacement text. |
| `replace_all` | boolean |  | false | — |
| `apply` | boolean |  | false | — |
| `base_sha256` | string |  |  | Optional optimistic-concurrency check. |
| `task` | string |  |  | Optional journal task label. |
### `dev_stats`

Snapshot histograms and repo summary.

Input schema: `{"type": "object", "properties": {}, "required": [], "additionalProperties": false}`

### `dev_supply_chain`

Run the offline supply-chain attack checks over dependency inputs: untrusted registries, credentials, TLS settings, mutable/direct sources, wildcard versions, mixed lockfile integrity, install/build scripts, typosquat and dependency-confusion candidates. Read-only: no network, install, target import or code execution. A clean result is not a verdict.

| parameter | type | required | default | description |
|---|---|---|---|---|
| `action` | string |  | "check" | — |
| `policy` | string |  |  | Optional policy JSON path inside the workspace. |
| `rule` | string |  |  | Rule id for action=explain. |
| `budget_s` | number |  | 10 | Time budget in seconds (maximum 60). |
| `max_findings` | integer |  | 50 | Finding cap (maximum 100). |
### `dev_symbol`

Find a function/class/struct/enum by name or qualname. Returns its file, line range, docstring, source snippet, tests and graph neighbours.

| parameter | type | required | default | description |
|---|---|---|---|---|
| `name` | string | yes |  | Exact or partial symbol name. |
### `dev_symbol_callees`

Symbols that a symbol calls or references, from the syntactic symbol graph (local, import and name resolution, each edge with a confidence). symbol accepts "path::qualname", a qualname ("Class.method"), or a bare name. A bare or qualname that matches several definitions returns the candidate ids (under "ambiguous", or "matches" for dev_symbol_resolve). Line numbers (lsp_only rows) are 1-based.

| parameter | type | required | default | description |
|---|---|---|---|---|
| `symbol` | string | yes |  | "path::qualname", qualname, or bare name. |
| `depth` | integer |  | 1 | — |
| `min_confidence` | number |  | 0 | — |
### `dev_symbol_callers`

Symbol callers from the syntactic symbol graph (local, import and name resolution, each edge with a confidence). symbol accepts "path::qualname", a qualname ("Class.method"), or a bare name. A bare or qualname that matches several definitions returns the candidate ids (under "ambiguous", or "matches" for dev_symbol_resolve). Line numbers (lsp_only rows) are 1-based. Use precise=true to confirm direct callers with LSP references; rows then carry lsp_confirmed and LSP-only references are listed under lsp_only.

| parameter | type | required | default | description |
|---|---|---|---|---|
| `symbol` | string | yes |  | "path::qualname", qualname, or bare name. |
| `depth` | integer |  | 1 | — |
| `min_confidence` | number |  | 0 | — |
| `precise` | boolean |  | false | Confirm depth-1 callers with LSP references (requires a language server). |
### `dev_symbol_graph_impact`

Transitive callers of a symbol (what breaks if it changes), grouped by file, from the syntactic symbol graph with per-edge confidences. symbol accepts "path::qualname", a qualname ("Class.method"), or a bare name. A bare or qualname that matches several definitions returns the candidate ids (under "ambiguous", or "matches" for dev_symbol_resolve). Line numbers (lsp_only rows) are 1-based. Use precise=true to confirm depth-1 callers with LSP references.

| parameter | type | required | default | description |
|---|---|---|---|---|
| `symbol` | string | yes |  | "path::qualname", qualname, or bare name. |
| `depth` | integer |  | 3 | — |
| `min_confidence` | number |  | 0 | — |
| `precise` | boolean |  | false | Confirm depth-1 callers with LSP references (requires a language server). |
### `dev_symbol_impact`

Resolved references grouped by file for a symbol position via LSP. Positions are 0-based LSP (line and character); dev_symbol and dev_file report 1-based lines, so subtract 1.

| parameter | type | required | default | description |
|---|---|---|---|---|
| `file` | string | yes |  | — |
| `line` | integer | yes |  | 0-based line (LSP-native). |
| `character` | integer | yes |  | 0-based UTF-16 character (LSP-native). |
| `language` | string |  |  | — |
### `dev_symbol_resolve`

Resolve a symbol query to its definition ids without walking the graph. symbol accepts "path::qualname", a qualname ("Class.method"), or a bare name. A bare or qualname that matches several definitions returns the candidate ids (under "ambiguous", or "matches" for dev_symbol_resolve). Line numbers (lsp_only rows) are 1-based.

| parameter | type | required | default | description |
|---|---|---|---|---|
| `symbol` | string | yes |  | "path::qualname", qualname, or bare name. |
### `dev_tests`

Tests covering a file, symbol or seed. Use before changing a module/seed to know the scoped gate, and after a change to record the verification command with dev_record.

| parameter | type | required | default | description |
|---|---|---|---|---|
| `target` | string | yes |  | File path, symbol name, or seed id. |
### `dev_tools`

Use before PDF/document/media/browser work to see installed/missing tools and approval/install previews. Pass capabilities (pdf, ocr, office, image, audio, video, archive, diagram, data, browser, ops, design, communication; or groups documents, media, web) and/or paths whose extensions infer them. Reports the environment, managers, installed tools with versions when probed, missing tools and the package-manager commands that would install them (would_run is always false). It never installs, never uses the network and never calls a model; version and sudo probes are off by default.

| parameter | type | required | default | description |
|---|---|---|---|---|
| `capabilities` | array |  |  | Capability or group ids, e.g. pdf or documents. |
| `paths` | array |  |  | Root-relative files whose extensions infer a capability, e.g. report.pdf. |
| `model_family` | string |  |  | claude, codex, gemini, cursor, aider, openhands or local; picks that family's preferred tool in the text. |
| `probe_versions` | boolean |  | false | Run bounded --version probes (default: no). |
| `probe_sudo` | boolean |  | false | Run the sudo -n true usability check (default: no). |
### `dev_who`

Which agent sessions edited a file (harness and session id, edit count, first and last edit), whether each session is live (seen by the ozagia daemon in the last 15 minutes), and whether its edits are committed (the file is clean, or committed after the edit). Committed is judged per file, not per hunk: a partial commit of the file (git add -p) by anyone counts the edit as committed even if its hunk is still dirty. Check it before editing a file other sessions may be changing in the same checkout.

| parameter | type | required | default | description |
|---|---|---|---|---|
| `path` | string | yes |  | Repo-relative or absolute file path. |
### `dev_working_set`

What an agent session was working on, kept by the ozagia daemon across context compaction: files and node keys it edited, tests that failed in its last pytest runs (with the command that reruns them), running background jobs, the current DevSession step, and the files and line ranges it read last. Use it after a compaction or when resuming, instead of re-reading files. Without session it answers for the most recently active session in this repository, of any harness unless harness is given (named in the header; it may not be yours); since=true gives the session-start brief (dirty files, recent commits, the root's recorded failing tests, running jobs).

| parameter | type | required | default | description |
|---|---|---|---|---|
| `session` | string |  |  | The harness's session id (default: the most recently active session, named in the header). |
| `harness` | string |  |  | opencode, claude, codex or kiro (default: any). |
| `since` | boolean |  | false | The session-start brief instead of a session's working set. |
### `federation_check`

Read-only static checks for declared cross-repository API and code-to-infrastructure relationships. Designated references may be fetched and fast-forwarded over Git; set no_sync to opt out. Indexes are not refreshed; missing evidence remains unknown.

| parameter | type | required | default | description |
|---|---|---|---|---|
| `roots` | array |  |  | Optional explicit member roots; an empty array selects only the anchor. |
| `no_sync` | boolean |  | false | — |
### `federation_node`

Read-only inspection of a qualified federated node and its snapshot-local edges. Use the qualified key returned by federation search. Designated references may be fetched and fast-forwarded; set no_sync to opt out. Node inspection does not infer links.

| parameter | type | required | default | description |
|---|---|---|---|---|
| `qualified_key` | string | yes |  | — |
| `roots` | array |  |  | Optional explicit member roots. |
| `no_sync` | boolean |  | false | — |
### `federation_search`

Read-only lexical search over discovered or explicitly selected repository snapshots. Designated references may be fetched and fast-forwarded over Git; set no_sync to opt out. Results retain provenance; no index is refreshed.

| parameter | type | required | default | description |
|---|---|---|---|---|
| `query` | string | yes |  | — |
| `roots` | array |  |  | Optional explicit member roots. |
| `limit` | integer |  | 20 | — |
| `no_sync` | boolean |  | false | — |
### `federation_status`

Read-only status of indexed repositories in the bounded sibling federation, or in explicitly supplied roots. Reports membership and snapshot freshness. Only paths explicitly listed in the anchor manifest's reference_repos may be fetched and fast-forwarded; this uses the network and changes those clean checkouts. Set no_sync to true to opt out. Indexes are not refreshed.

| parameter | type | required | default | description |
|---|---|---|---|---|
| `roots` | array |  |  | Optional explicit member roots. |
| `no_sync` | boolean |  | false | — |
### `goal_add`

Create a goal. targets are graph keys (file:, sym:, test:, doc:, ops:); scope keys or path globs; criteria strings 'kind:args' or objects {kind, args}. Kinds: test_passes, test_fails, symbol_exists, symbol_absent, calls, not_calls, no_callers, file_exists, file_absent, unchanged, lint_clean, review_resolved, ops_has. Unknown keys are refused, except the subject of symbol_exists/file_exists. ops: keys also resolve from the tracked ops sources; ops_has takes pinned: true. review_resolved {packet, finding_id} needs a saved review packet under .ozagia/reviews/ that contains the finding, and passes only on a later review (goal_from_review is simpler).

| parameter | type | required | default | description |
|---|---|---|---|---|
| `title` | string | yes |  | — |
| `why` | string |  |  | Free text. |
| `targets` | array |  |  | — |
| `scope` | array |  |  | — |
| `criteria` | array | yes |  | 'kind:args' strings or {kind, args} objects. |
### `goal_check`

Evaluate goals' criteria from the graph, the working tree and the test record (never from the agent's claim), record the results, and move a goal to met when every criterion passes or to regressed when a met goal fails. A test result older than the last change to the goal's targets is stale: rerun the test.

| parameter | type | required | default | description |
|---|---|---|---|---|
| `ids` | array |  |  | Goal ids (default: every goal not dropped). |
### `goal_claim`

Claim a goal for your harness session (or release it). After an edit, the daemon then names the goal when the edit touches its targets and notes edits outside its scope (advisory).

| parameter | type | required | default | description |
|---|---|---|---|---|
| `id` | string | yes |  | — |
| `harness` | string | yes |  | opencode, claude, codex or kiro. |
| `session` | string | yes |  | Your session id. |
| `release` | boolean |  | false | — |
### `goal_from_review`

Turn a finding of the saved whole-repo review (dev_review_repo; .ozagia/reviews/latest.json) into a goal: its suggested targets and criteria plus review_resolved, which passes only when a later review no longer reports the finding.

| parameter | type | required | default | description |
|---|---|---|---|---|
| `finding_id` | string | yes |  | A finding id from dev_review_repo, e.g. RR-1a2b... |
### `goal_list`

List this repository's goals (graph-addressed work items): id, title, status (open, met, regressed, blocked, dropped), who claimed them, targets and the criteria not passing at the last check. Read-only; goal_check re-evaluates.

| parameter | type | required | default | description |
|---|---|---|---|---|
| `status` | string |  |  | Only goals with this status. |
| `all` | boolean |  | false | Include dropped goals. |
### `goal_show`

One goal in full: targets, scope, each criterion with its last result and evidence, claims and history.

| parameter | type | required | default | description |
|---|---|---|---|---|
| `id` | string | yes |  | Goal id, e.g. g1. |
### `job_list`

List background jobs (newest first): id, command, status, exit code, pid, elapsed seconds, log path.

Input schema: `{"type": "object", "properties": {}, "required": [], "additionalProperties": false}`

### `job_log`

Read a job's log lines you have not seen yet (or from cursor; cursor 0 gives the last lines of the whole log), without waiting.

| parameter | type | required | default | description |
|---|---|---|---|---|
| `job` | string | yes |  | A job id from job_start or job_list (e.g. "j3"). |
| `cursor` | integer |  |  | Byte offset to read from. |
| `tail` | integer |  | 200 | Most lines to return (the newest). |
### `job_start`

Start a long-running command (a server, a build, a slow test run) in the background, in its own process group, with stdout and stderr in a log. Use it instead of `cmd &`, nohup, or a shell session you poll; then job_wait for its exit or a log line. Give command (a shell string) or argv. Jobs are stopped when the ozagia daemon exits unless detach is true. Jobs run in the ozagia daemon with its privileges and environment, outside the Codex sandbox, approval prompts and shell environment policy (and OpenCode's permission checks). Starting them is off unless the project enables it ([jobs] enabled = true in .ozagia.toml).

| parameter | type | required | default | description |
|---|---|---|---|---|
| `command` | string |  |  | Shell command line, run with bash -c. |
| `argv` | array |  |  | Program and arguments, run without a shell. |
| `cwd` | string |  |  | Working directory inside the project (default: its root). |
| `env` | object |  |  | Environment variables to add. A value may be a secret:// reference resolved by the daemon at spawn; use a reference for credentials. |
| `detach` | boolean |  | false | Leave it running when the daemon exits. |
### `job_stop`

Stop a job: the signal to its whole process group, then SIGKILL after grace_s.

| parameter | type | required | default | description |
|---|---|---|---|---|
| `job` | string | yes |  | A job id from job_start or job_list (e.g. "j3"). |
| `signal` | string |  | "TERM" | — |
| `grace_s` | number |  | 3 | Seconds before SIGKILL (at most 10). |
### `job_wait`

Block until a job exits, a file exists, or a new log line matches a regex, or until the deadline; returns the status, the exit code, and only the log lines you have not seen yet (the newest 200 lines / 16 kB at most). Use it instead of sleep loops, ps or polling a shell. If it returns with the job still running, call it again: each call continues where the last one stopped.

| parameter | type | required | default | description |
|---|---|---|---|---|
| `job` | string |  | "any" | Job id, or "any" for the first running job to finish (with until file:<path> and no job, waits for the file alone). |
| `until` | string |  | "exit" | exit \| file:<path> \| log:<regex>. |
| `deadline_s` | number |  | 45.0 | Seconds to wait (at most 50). |
| `cursor` | integer |  |  | Byte offset in the log to read from (default: where your last read ended). |
### `research_alert_ack`

Acknowledge a living-review alert.

| parameter | type | required | default | description |
|---|---|---|---|---|
| `session_id` | string | yes |  | — |
| `alert_id` | string | yes |  | — |
| `reviewer` | string | yes |  | — |
| `note` | string |  |  | — |
### `research_alert_record`

Record a living-review alert against a target.

| parameter | type | required | default | description |
|---|---|---|---|---|
| `session_id` | string | yes |  | — |
| `kind` | string | yes |  | — |
| `target_id` | string | yes |  | — |
| `payload` |  |  |  | — |
| `reviewer` | string |  |  | — |
| `status` | string |  |  | — |
### `research_alert_report`

Report living-review alerts by kind and status.

| parameter | type | required | default | description |
|---|---|---|---|---|
| `session_id` | string | yes |  | — |
| `status` | string |  |  | — |
### `research_alert_scan`

Scan recorded metadata for retractions, unresolved citations and new citations.

| parameter | type | required | default | description |
|---|---|---|---|---|
| `session_id` | string | yes |  | — |
| `review_plan_id` | string |  |  | — |
| `since` | string |  |  | — |
| `reference_ids` | array |  |  | — |
### `research_appraisal_table`

Export risk-of-bias and certainty rows as Markdown or CSV.

| parameter | type | required | default | description |
|---|---|---|---|---|
| `session_id` | string | yes |  | — |
| `format` | string |  |  | — |
### `research_argument_brief`

Build a deterministic steelman/red-team/synthesis brief for a claim.

| parameter | type | required | default | description |
|---|---|---|---|---|
| `session_id` | string | yes |  | — |
| `claim_id` | string | yes |  | — |
| `mode` | string |  |  | — |
### `research_argument_check`

Check premise evidence, citations, objection replies and falsifiers.

| parameter | type | required | default | description |
|---|---|---|---|---|
| `session_id` | string | yes |  | — |
| `argument_id` | string | yes |  | — |
### `research_argument_record`

Record a candidate steelman/red-team/synthesis argument with evidence obligations.

| parameter | type | required | default | description |
|---|---|---|---|---|
| `session_id` | string | yes |  | — |
| `claim_id` | string | yes |  | — |
| `mode` | string | yes |  | — |
| `thesis` | string | yes |  | — |
| `premises` | array | yes |  | — |
| `assumptions` | array |  |  | — |
| `objections` | array |  |  | — |
| `falsifiers` | array |  |  | — |
| `confidence` | number | yes |  | — |
| `reviewer` | string | yes |  | — |
| `model_runs` |  |  |  | — |
| `notes` | string |  |  | — |
### `research_argument_report`

Return an argument with its linked evidence and checks.

| parameter | type | required | default | description |
|---|---|---|---|---|
| `session_id` | string | yes |  | — |
| `argument_id` | string | yes |  | — |
### `research_certainty_record`

Record a reviewer certainty rating with reasons and adjustments.

| parameter | type | required | default | description |
|---|---|---|---|---|
| `session_id` | string | yes |  | — |
| `target_id` | string | yes |  | — |
| `rating` | string | yes |  | — |
| `reasons` | array | yes |  | — |
| `reviewer` | string | yes |  | — |
| `framework` | string |  |  | — |
| `downgrades` | array |  |  | — |
| `upgrades` | array |  |  | — |
| `notes` | string |  |  | — |
### `research_certainty_report`

Report certainty ratings, risk-of-bias summaries and warnings.

| parameter | type | required | default | description |
|---|---|---|---|---|
| `session_id` | string | yes |  | — |
| `target_id` | string |  |  | — |
### `research_check`

Check research records and provenance; does not certify truth.

| parameter | type | required | default | description |
|---|---|---|---|---|
| `session_id` | string | yes |  | — |
### `research_check_lean`

Check generated Lean declarations with the local Lean executable.

| parameter | type | required | default | description |
|---|---|---|---|---|
| `session_id` | string | yes |  | — |
| `node_id` | string |  |  | — |
### `research_citation_resolution_report`

Report resolved and unresolved citation hints by method.

| parameter | type | required | default | description |
|---|---|---|---|---|
| `session_id` | string | yes |  | — |
| `claim_id` | string |  |  | — |
### `research_citation_resolve`

Resolve a claim's numeric, author-year or DOI/arXiv citation hints to references.

| parameter | type | required | default | description |
|---|---|---|---|---|
| `session_id` | string | yes |  | — |
| `claim_id` | string | yes |  | — |
| `apply` | boolean |  |  | — |
| `threshold` | number |  |  | — |
### `research_cite_context`

Extract sentence/line citation contexts as candidates.

| parameter | type | required | default | description |
|---|---|---|---|---|
| `session_id` | string | yes |  | — |
| `root_id` | string | yes |  | — |
| `path` | string | yes |  | — |
| `record` | boolean |  |  | — |
### `research_claim_holding`

Show the holdings and defaults for the sources a claim relies on.

| parameter | type | required | default | description |
|---|---|---|---|---|
| `session_id` | string | yes |  | — |
| `claim_id` | string | yes |  | — |
### `research_context`

Find cited local research context, plus any user positions on the same topic or records (reported apart from evidence).

| parameter | type | required | default | description |
|---|---|---|---|---|
| `session_id` | string | yes |  | — |
| `query` | string | yes |  | — |
| `limit` | integer |  |  | — |
| `node_ids` | array |  |  | — |
### `research_contradiction_check`

Check a recorded contradiction's claims, edges and status.

| parameter | type | required | default | description |
|---|---|---|---|---|
| `session_id` | string | yes |  | — |
| `contradiction_id` | string | yes |  | — |
### `research_contradiction_record`

Record a reviewed contradiction candidate between two claims.

| parameter | type | required | default | description |
|---|---|---|---|---|
| `session_id` | string | yes |  | — |
| `claim_a_id` | string | yes |  | — |
| `claim_b_id` | string | yes |  | — |
| `kind` | string | yes |  | — |
| `rationale` | string | yes |  | — |
| `evidence_ids` | array |  |  | — |
| `reviewer` | string | yes |  | — |
| `status` | string |  |  | — |
### `research_contradiction_report`

Report contradiction candidates and recorded contradictions.

| parameter | type | required | default | description |
|---|---|---|---|---|
| `session_id` | string | yes |  | — |
### `research_contradiction_scan`

Find deterministic contradiction candidates across claims and extractions.

| parameter | type | required | default | description |
|---|---|---|---|---|
| `session_id` | string | yes |  | — |
| `max_pairs` | integer |  |  | — |
### `research_document_claim_ingest`

Record a note, HTML/website or EPUB Source, holding and claim candidates.

| parameter | type | required | default | description |
|---|---|---|---|---|
| `session_id` | string | yes |  | — |
| `root_id` | string | yes |  | — |
| `path` | string | yes |  | — |
| `source_type` | string |  |  | — |
| `title` | string |  |  | — |
| `metadata` | object |  |  | — |
| `max_claims` | integer |  |  | — |
| `reviewer` | string |  |  | — |
| `holding` |  |  |  | — |
| `resolve_citations` | boolean |  |  | — |
### `research_document_claim_preview`

Parse a selected note, HTML/website snapshot or EPUB book into claim candidates.

| parameter | type | required | default | description |
|---|---|---|---|---|
| `session_id` | string | yes |  | — |
| `root_id` | string | yes |  | — |
| `path` | string | yes |  | — |
| `source_type` | string |  |  | — |
| `title` | string |  |  | — |
| `metadata` | object |  |  | — |
| `max_claims` | integer |  |  | — |
### `research_evidence_table`

Project claims and evidence into a Markdown or CSV table.

| parameter | type | required | default | description |
|---|---|---|---|---|
| `session_id` | string | yes |  | — |
| `format` | string |  |  | — |
### `research_execute`

Run explicit graph-bound native substrate terms.

| parameter | type | required | default | description |
|---|---|---|---|---|
| `session_id` | string | yes |  | — |
| `node_id` | string | yes |  | — |
| `env` | object |  |  | — |
| `max_depth` | integer |  |  | — |
| `max_nodes` | integer |  |  | — |
### `research_export`

Export an auditable ledger and JSON bundle.

| parameter | type | required | default | description |
|---|---|---|---|---|
| `session_id` | string | yes |  | — |
### `research_export_lean`

Export explicit Boolean terms as Lean definitions.

| parameter | type | required | default | description |
|---|---|---|---|---|
| `session_id` | string | yes |  | — |
| `node_id` | string |  |  | — |
### `research_external_search`

Search through an explicitly named external provider; results are unverified candidates.

| parameter | type | required | default | description |
|---|---|---|---|---|
| `session_id` | string | yes |  | — |
| `query` | string | yes |  | — |
| `provider` | string | yes |  | — |
| `limit` | integer |  |  | — |
### `research_extraction_check`

Check an extraction's target, fields, numerics and locator.

| parameter | type | required | default | description |
|---|---|---|---|---|
| `session_id` | string | yes |  | — |
| `extraction_id` | string | yes |  | — |
### `research_extraction_record`

Record structured study fields and effect sizes for a Reference or Claim.

| parameter | type | required | default | description |
|---|---|---|---|---|
| `session_id` | string | yes |  | — |
| `reference_id` | string |  |  | — |
| `claim_id` | string |  |  | — |
| `fields` | object | yes |  | — |
| `source_locator` | object |  |  | — |
| `extractor` | string | yes |  | — |
| `reviewer` | string |  |  | — |
| `status` | string |  |  | — |
### `research_extraction_report`

Return an extraction with its target and checks.

| parameter | type | required | default | description |
|---|---|---|---|---|
| `session_id` | string | yes |  | — |
| `extraction_id` | string | yes |  | — |
### `research_extraction_table`

Export structured extractions as Markdown or CSV.

| parameter | type | required | default | description |
|---|---|---|---|---|
| `session_id` | string | yes |  | — |
| `format` | string |  |  | — |
### `research_fetch`

Fetch the text of a paper whose web fetch failed (404/403, a PDF the fetch tool can't read, a publisher landing page). Resolves open-access copies (arXiv, DOI via Crossref/OpenAlex/Unpaywall, PubMed/PMC via Europe PMC, Semantic Scholar, OpenReview, citation_pdf_url on a landing page), downloads with a size cap, extracts PDF or JATS text, caches it by content hash and returns it page by page with a stable source: key. Pass offset=next_offset for the next part, refresh=true to resolve again instead of using the cache. Open access only; network is off with OZAGIA_RESEARCH_OFFLINE=1 (cached copies are still served).

| parameter | type | required | default | description |
|---|---|---|---|---|
| `url` | string | yes |  | The URL that failed. |
| `max_chars` | integer |  |  | Characters to return (default 20000, at most 100000). |
| `offset` | integer |  |  | Character offset into the document text (default 0). |
| `refresh` | boolean |  |  | Resolve the URL again instead of answering from the cache (default false). |
### `research_fetch_apply`

Download one selected artifact, hash it and record a Source.

| parameter | type | required | default | description |
|---|---|---|---|---|
| `session_id` | string | yes |  | — |
| `reference_id` | string | yes |  | — |
| `candidate` | object | yes |  | — |
### `research_fetch_preview`

List open-access artifact candidates for a Reference or identifier.

| parameter | type | required | default | description |
|---|---|---|---|---|
| `session_id` | string | yes |  | — |
| `reference_id` | string |  |  | — |
| `identifier` | string |  |  | — |
| `reference` | object |  |  | — |
| `email` | string |  |  | — |
| `provider` | string |  |  | — |
### `research_formula_model`

Translate a formula and attach it as a reviewable claim model.

| parameter | type | required | default | description |
|---|---|---|---|---|
| `session_id` | string | yes |  | — |
| `claim_id` | string | yes |  | — |
| `formula` | string | yes |  | — |
| `domain_assumptions` | object |  |  | — |
| `context_scope_id` | string |  |  | — |
| `bindings` | object |  |  | — |
| `assumption_ids` | array |  |  | — |
| `residuals` | array |  |  | — |
| `reviewer` | string | yes |  | — |
| `review_note` | string | yes |  | — |
### `research_formula_parse_candidates`

Optionally parse supplied LaTeX formulas with SymPy/Lark; returns unverified candidates without execution or recording.

| parameter | type | required | default | description |
|---|---|---|---|---|
| `session_id` | string | yes |  | — |
| `formulas` | array | yes |  | — |
### `research_formula_resolve`

Use scoped research graph context to compare formula interpretations.

| parameter | type | required | default | description |
|---|---|---|---|---|
| `session_id` | string | yes |  | — |
| `formula` | string | yes |  | — |
| `scope_id` | string | yes |  | — |
### `research_formula_translate`

Translate a supported formula into a native executable Term.

| parameter | type | required | default | description |
|---|---|---|---|---|
| `session_id` | string | yes |  | — |
| `formula` | string | yes |  | — |
| `domain_assumptions` | object |  |  | — |
### `research_holding_check`

Check a holding's source, defaults and format-specific warnings.

| parameter | type | required | default | description |
|---|---|---|---|---|
| `session_id` | string | yes |  | — |
| `holding_id` | string | yes |  | — |
### `research_holding_record`

Record how a source should be held: evidence, interpretation, hypothesis, note, background, etc.

| parameter | type | required | default | description |
|---|---|---|---|---|
| `session_id` | string | yes |  | — |
| `source_id` | string | yes |  | — |
| `source_type` | string | yes |  | — |
| `epistemic_role` | string |  |  | — |
| `authority` | string |  |  | — |
| `review_status` | string |  |  | — |
| `use_constraints` | array |  |  | — |
| `scope` | string |  |  | — |
| `rationale` | string |  |  | — |
| `reviewer` | string |  |  | — |
| `status` | string |  |  | — |
| `confidence` | number |  |  | — |
### `research_holding_report`

Report holdings by source type, epistemic role, authority and status.

| parameter | type | required | default | description |
|---|---|---|---|---|
| `session_id` | string | yes |  | — |
| `source_id` | string |  |  | — |
### `research_init`

Create an investigation.

| parameter | type | required | default | description |
|---|---|---|---|---|
| `question` | string | yes |  | — |
| `mode` | string |  |  | — |
| `roots` | array |  |  | — |
### `research_inspect`

Inspect a session or a record and its links.

| parameter | type | required | default | description |
|---|---|---|---|---|
| `session_id` | string | yes |  | — |
| `node_id` | string |  |  | — |
| `transitive` | boolean |  |  | — |
### `research_link`

Link two research records.

| parameter | type | required | default | description |
|---|---|---|---|---|
| `session_id` | string | yes |  | — |
| `src` | string | yes |  | — |
| `relation` | string | yes |  | — |
| `tgt` | string | yes |  | — |
| `fields` | object |  |  | — |
### `research_manuscript_bundle`

Write a manuscript bundle with BibTeX, claims CSV and checks; optionally run Pandoc.

| parameter | type | required | default | description |
|---|---|---|---|---|
| `session_id` | string | yes |  | — |
| `title` | string | yes |  | — |
| `body` | string | yes |  | — |
| `output_dir` | string |  |  | — |
| `run_pandoc` | boolean |  |  | — |
| `pandoc_format` | string |  |  | — |
### `research_manuscript_check`

Check a manuscript body for unresolved citations, retracted references and unsourced claims.

| parameter | type | required | default | description |
|---|---|---|---|---|
| `session_id` | string | yes |  | — |
| `body` | string | yes |  | — |
| `require_claim_sources` | boolean |  |  | — |
### `research_model_candidate`

Attach an explicit native Term model candidate to a sourced claim.

| parameter | type | required | default | description |
|---|---|---|---|---|
| `session_id` | string | yes |  | — |
| `claim_id` | string | yes |  | — |
| `term` | object | yes |  | — |
| `bindings` | object |  |  | — |
| `assumption_ids` | array |  |  | — |
| `residuals` | array | yes |  | — |
| `reviewer` | string | yes |  | — |
| `review_note` | string | yes |  | — |
| `provenance` | object |  |  | — |
### `research_native_graph`

Project the research graph and optionally evaluate a native DSL term.

| parameter | type | required | default | description |
|---|---|---|---|---|
| `session_id` | string | yes |  | — |
| `query` | object |  |  | — |
| `env` | object |  |  | — |
### `research_paper_ingest`

Record a selected paper's claims and references in the research graph.

| parameter | type | required | default | description |
|---|---|---|---|---|
| `session_id` | string | yes |  | — |
| `root_id` | string | yes |  | — |
| `path` | string | yes |  | — |
| `max_formula_candidates` | integer |  |  | — |
| `formula_offset` | integer |  |  | — |
| `latexml_path` | string |  |  | — |
| `bibliography_path` | string |  |  | — |
### `research_paper_preview`

Read a selected paper into source-linked claim candidates without recording.

| parameter | type | required | default | description |
|---|---|---|---|---|
| `session_id` | string | yes |  | — |
| `root_id` | string | yes |  | — |
| `path` | string | yes |  | — |
| `max_formula_candidates` | integer |  |  | — |
| `formula_offset` | integer |  |  | — |
| `latexml_path` | string |  |  | — |
| `bibliography_path` | string |  |  | — |
### `research_paper_review_bundle`

Write review.md, review.json, claims/evidence CSVs, BibTeX, arguments and checks.

| parameter | type | required | default | description |
|---|---|---|---|---|
| `session_id` | string | yes |  | — |
| `plan_id` | string | yes |  | — |
| `output_dir` | string |  |  | — |
### `research_paper_review_packet`

Build a complete deterministic packet for reviewing a paper Source.

| parameter | type | required | default | description |
|---|---|---|---|---|
| `session_id` | string | yes |  | — |
| `source_id` | string | yes |  | — |
| `plan_id` | string |  |  | — |
| `manuscript_body` | string |  |  | — |
| `max_claims` | integer |  |  | — |
### `research_paper_review_report`

Build the structured paper review report from recorded runs, findings, arguments and checks.

| parameter | type | required | default | description |
|---|---|---|---|---|
| `session_id` | string | yes |  | — |
| `plan_id` | string | yes |  | — |
### `research_paper_review_start`

Ingest a selected paper, create a review plan targeting its Source, and build the review packet.

| parameter | type | required | default | description |
|---|---|---|---|---|
| `session_id` | string | yes |  | — |
| `root_id` | string | yes |  | — |
| `path` | string | yes |  | — |
| `question` | string | yes |  | — |
| `roles` | array | yes |  | — |
| `models` | array | yes |  | — |
| `kind` | string |  |  | — |
| `bibliography_path` | string |  |  | — |
| `criteria` |  |  |  | — |
| `min_reviewers` | integer |  |  | — |
| `reviewer` | string |  |  | — |
### `research_pdf_claim_ingest`

Record PDF Source, claim candidates, GROBID references and best-effort citation links.

| parameter | type | required | default | description |
|---|---|---|---|---|
| `session_id` | string | yes |  | — |
| `root_id` | string | yes |  | — |
| `path` | string | yes |  | — |
| `backend` | string |  |  | — |
| `max_claims` | integer |  |  | — |
| `reviewer` | string |  |  | — |
| `resolve_citations` | boolean |  |  | — |
### `research_pdf_claim_preview`

Extract formal, abstract, contribution and definition claim candidates from a selected PDF without recording.

| parameter | type | required | default | description |
|---|---|---|---|---|
| `session_id` | string | yes |  | — |
| `root_id` | string | yes |  | — |
| `path` | string | yes |  | — |
| `backend` | string |  |  | — |
| `max_claims` | integer |  |  | — |
### `research_pdf_preview`

Extract page text, OCR or GROBID TEI from a PDF; optionally extract claim candidates.

| parameter | type | required | default | description |
|---|---|---|---|---|
| `session_id` | string | yes |  | — |
| `root_id` | string |  |  | — |
| `path` | string |  |  | — |
| `source_id` | string |  |  | — |
| `backend` | string |  |  | — |
| `record_references` | boolean |  |  | — |
| `extract_claims` | boolean |  |  | — |
| `ocr` |  |  |  | — |
| `ocr_lang` | string |  |  | — |
| `ocr_dpi` | integer |  |  | — |
### `research_position_list`

List user positions by status (active, superseded, retracted, all), record or document.

| parameter | type | required | default | description |
|---|---|---|---|---|
| `session_id` | string | yes |  | — |
| `status` | string |  |  | — |
| `node_id` | string |  |  | — |
| `document` | string |  |  | — |
### `research_position_record`

Record the user's stated stance or correction as a user position sourced to this conversation, linked to the topic, records and documents it concerns. Store only what the user stated or approved.

| parameter | type | required | default | description |
|---|---|---|---|---|
| `session_id` | string | yes |  | — |
| `stance` | string | yes |  | — |
| `conversation_session_id` | string | yes |  | — |
| `topic` | string |  |  | — |
| `quote` | string |  |  | — |
| `stated_at` | string |  |  | — |
| `concerns` | array |  |  | — |
| `documents` | array |  |  | — |
| `correction_of` | string |  |  | — |
### `research_position_retract`

Retract an active user position; the record and its history remain.

| parameter | type | required | default | description |
|---|---|---|---|---|
| `session_id` | string | yes |  | — |
| `position_id` | string | yes |  | — |
| `conversation_session_id` | string | yes |  | — |
| `note` | string |  |  | — |
### `research_position_supersede`

Record a new user position that replaces an active one; topic, concerns and documents carry over when omitted.

| parameter | type | required | default | description |
|---|---|---|---|---|
| `session_id` | string | yes |  | — |
| `position_id` | string | yes |  | — |
| `stance` | string | yes |  | — |
| `conversation_session_id` | string | yes |  | — |
| `topic` | string |  |  | — |
| `quote` | string |  |  | — |
| `stated_at` | string |  |  | — |
| `concerns` | array |  |  | — |
| `documents` | array |  |  | — |
| `correction_of` | string |  |  | — |
### `research_record`

Append a research record.

| parameter | type | required | default | description |
|---|---|---|---|---|
| `session_id` | string | yes |  | — |
| `kind` | string | yes |  | — |
| `fields` | object | yes |  | — |
### `research_ref_check`

Check citation keys, duplicates, metadata gaps and recorded retractions.

| parameter | type | required | default | description |
|---|---|---|---|---|
| `session_id` | string | yes |  | — |
### `research_ref_citation_fetch`

Fetch bounded OpenAlex references or citing works and record cites_work edges.

| parameter | type | required | default | description |
|---|---|---|---|---|
| `session_id` | string | yes |  | — |
| `reference_id` | string | yes |  | — |
| `direction` | string |  |  | — |
| `limit` | integer |  |  | — |
| `apply` | boolean |  |  | — |
### `research_ref_citation_neighborhood`

Traverse recorded cites_work edges around a Reference.

| parameter | type | required | default | description |
|---|---|---|---|---|
| `session_id` | string | yes |  | — |
| `reference_id` | string | yes |  | — |
| `depth` | integer |  |  | — |
| `direction` | string |  |  | — |
### `research_ref_citation_path`

Find a directed cites_work path between two References.

| parameter | type | required | default | description |
|---|---|---|---|---|
| `session_id` | string | yes |  | — |
| `src` | string | yes |  | — |
| `tgt` | string | yes |  | — |
| `max_depth` | integer |  |  | — |
### `research_ref_export`

Export recorded references as BibTeX or CSL-JSON.

| parameter | type | required | default | description |
|---|---|---|---|---|
| `session_id` | string | yes |  | — |
| `format` | string |  |  | — |
### `research_ref_import`

Record a selected bibliography as bibliographic Reference nodes.

| parameter | type | required | default | description |
|---|---|---|---|---|
| `session_id` | string | yes |  | — |
| `root_id` | string | yes |  | — |
| `path` | string | yes |  | — |
### `research_ref_link_citations`

Link a record's stored citation keys to existing Reference nodes.

| parameter | type | required | default | description |
|---|---|---|---|---|
| `session_id` | string | yes |  | — |
| `node_id` | string | yes |  | — |
### `research_ref_preview`

Parse a selected BibTeX, BBL, CSL-JSON or RIS file without recording.

| parameter | type | required | default | description |
|---|---|---|---|---|
| `session_id` | string | yes |  | — |
| `root_id` | string | yes |  | — |
| `path` | string | yes |  | — |
### `research_ref_providers`

List external providers registered for opt-in use; none are enabled by default.

| parameter | type | required | default | description |
|---|---|---|---|---|
| `session_id` | string | yes |  | — |
### `research_ref_record`

Record or enrich one explicit bibliographic Reference.

| parameter | type | required | default | description |
|---|---|---|---|---|
| `session_id` | string | yes |  | — |
| `reference` | object | yes |  | — |
| `source_locator` | object |  |  | — |
### `research_ref_resolve`

Resolve DOI/arXiv/PMID/PMCID metadata from an open provider without recording.

| parameter | type | required | default | description |
|---|---|---|---|---|
| `session_id` | string | yes |  | — |
| `identifier` | string | yes |  | — |
| `email` | string |  |  | — |
| `provider` | string |  |  | — |
| `record` | boolean |  |  | — |
### `research_review_agreement`

Report Fleiss kappa, Jaccard, consensus and dissent across runs; optional embedding clusters.

| parameter | type | required | default | description |
|---|---|---|---|---|
| `session_id` | string | yes |  | — |
| `plan_id` | string | yes |  | — |
| `embedding_model` | string |  |  | — |
| `embedding_url` | string |  |  | — |
| `cluster_threshold` | number |  |  | — |
### `research_review_finding_add`

Add review findings to the cross-session ledger, explicitly or from a recorded review run; each has a document, anchor, severity and text.

| parameter | type | required | default | description |
|---|---|---|---|---|
| `session_id` | string | yes |  | — |
| `conversation_session_id` | string | yes |  | — |
| `findings` | array |  |  | — |
| `run_id` | string |  |  | — |
| `document` | string |  |  | — |
| `document_text` | string |  |  | — |
### `research_review_finding_status`

Mark a ledger finding open, addressed, wont_fix or disputed with a note; the change is kept in its history.

| parameter | type | required | default | description |
|---|---|---|---|---|
| `session_id` | string | yes |  | — |
| `finding_id` | string | yes |  | — |
| `status` | string | yes |  | — |
| `note` | string | yes |  | — |
| `conversation_session_id` | string | yes |  | — |
### `research_review_outstanding`

List still-open ledger findings grouped by document, most severe first.

| parameter | type | required | default | description |
|---|---|---|---|---|
| `session_id` | string | yes |  | — |
| `document` | string |  |  | — |
| `statuses` | array |  |  | — |
### `research_review_packet`

Build a deterministic evidence packet for a review target.

| parameter | type | required | default | description |
|---|---|---|---|---|
| `session_id` | string | yes |  | — |
| `plan_id` | string |  |  | — |
| `claim_id` | string |  |  | — |
| `reference_id` | string |  |  | — |
| `manuscript_body` | string |  |  | — |
### `research_review_plan_create`

Record a structured review plan with roles, models and criteria.

| parameter | type | required | default | description |
|---|---|---|---|---|
| `session_id` | string | yes |  | — |
| `question` | string | yes |  | — |
| `kind` | string |  |  | — |
| `roles` | array | yes |  | — |
| `models` | array | yes |  | — |
| `criteria` |  |  |  | — |
| `exclusion_criteria` |  |  |  | — |
| `sources` |  |  |  | — |
| `reviewers` | array |  |  | — |
| `rounds` | array |  |  | — |
| `blinding` | boolean |  |  | — |
| `min_reviewers` | integer |  |  | — |
| `adjudication` | string |  |  | — |
| `target_id` | string |  |  | — |
### `research_review_reconcile`

Record a reconciliation while preserving dissent; optional embedding or Ocelot structural clusters.

| parameter | type | required | default | description |
|---|---|---|---|---|
| `session_id` | string | yes |  | — |
| `plan_id` | string | yes |  | — |
| `method` | string |  |  | — |
| `min_reviewers` | integer |  |  | — |
| `adjudicator` |  |  |  | — |
| `rationale` | string |  |  | — |
| `run_ids` | array |  |  | — |
| `embedding_model` | string |  |  | — |
| `embedding_url` | string |  |  | — |
| `cluster_threshold` | number |  |  | — |
| `ocelot` | boolean |  |  | — |
| `ocelot_root` | string |  |  | — |
| `ocelot_python` | string |  |  | — |
| `ocelot_timeout` | integer |  |  | — |
| `ocelot_mode` | string |  |  | — |
### `research_review_report`

Return the plan, runs, findings and latest reconciliation.

| parameter | type | required | default | description |
|---|---|---|---|---|
| `session_id` | string | yes |  | — |
| `plan_id` | string | yes |  | — |
### `research_review_rereview`

Given a new version of a document, propose which open findings may now be addressed by checking whether their anchored text changed; never closes findings.

| parameter | type | required | default | description |
|---|---|---|---|---|
| `session_id` | string | yes |  | — |
| `document` | string | yes |  | — |
| `text` | string |  |  | — |
| `statuses` | array |  |  | — |
### `research_review_run_record`

Record one independent model/role review run and its validated findings.

| parameter | type | required | default | description |
|---|---|---|---|---|
| `session_id` | string | yes |  | — |
| `plan_id` | string | yes |  | — |
| `reviewer_id` | string | yes |  | — |
| `role` | string | yes |  | — |
| `model` | string | yes |  | — |
| `packet_sha256` | string | yes |  | — |
| `output` | object | yes |  | — |
| `provider` | string |  |  | — |
| `round` | string |  |  | — |
| `prompt_sha256` | string |  |  | — |
| `context_sha256` | string |  |  | — |
| `params` |  |  |  | — |
| `tokens` |  |  |  | — |
| `cost` | number |  |  | — |
| `status` | string |  |  | — |
| `target_id` | string |  |  | — |
### `research_review_search_log`

Record a search query and counts for a review plan.

| parameter | type | required | default | description |
|---|---|---|---|---|
| `session_id` | string | yes |  | — |
| `plan_id` | string | yes |  | — |
| `query` | string | yes |  | — |
| `provider` | string | yes |  | — |
| `executed_at` | string |  |  | — |
| `filters` |  |  |  | — |
| `result_count` | integer |  |  | — |
| `deduped_count` | integer |  |  | — |
| `export_sha256` | string |  |  | — |
| `notes` | string |  |  | — |
### `research_review_validate`

Validate one model review output against IDs, citations and packet quotes.

| parameter | type | required | default | description |
|---|---|---|---|---|
| `session_id` | string | yes |  | — |
| `output` | object | yes |  | — |
| `packet` | object |  |  | — |
### `research_risk_of_bias_check`

Check a risk-of-bias assessment's target, domains and support.

| parameter | type | required | default | description |
|---|---|---|---|---|
| `session_id` | string | yes |  | — |
| `assessment_id` | string | yes |  | — |
### `research_risk_of_bias_record`

Record one reviewer's risk-of-bias domains and overall judgement.

| parameter | type | required | default | description |
|---|---|---|---|---|
| `session_id` | string | yes |  | — |
| `target_id` | string | yes |  | — |
| `tool` | string | yes |  | — |
| `domains` | array | yes |  | — |
| `overall` | string | yes |  | — |
| `reviewer` | string | yes |  | — |
| `notes` | string |  |  | — |
### `research_screen`

Record a reviewable title/abstract or full-text screening decision.

| parameter | type | required | default | description |
|---|---|---|---|---|
| `session_id` | string | yes |  | — |
| `reference_id` | string | yes |  | — |
| `stage` | string | yes |  | — |
| `decision` | string | yes |  | — |
| `reason` | string | yes |  | — |
| `reviewer` | string | yes |  | — |
| `criteria` |  |  |  | — |
### `research_screening_report`

Count recorded screening decisions in a PRISMA-style report.

| parameter | type | required | default | description |
|---|---|---|---|---|
| `session_id` | string | yes |  | — |
### `research_search`

BM25 search over selected local research passages.

| parameter | type | required | default | description |
|---|---|---|---|---|
| `session_id` | string | yes |  | — |
| `query` | string | yes |  | — |
| `limit` | integer |  |  | — |
| `root_id` | string |  |  | — |
| `path` | string |  |  | — |
### `research_semantic_build`

Build the optional local vector sidecar; hash-bow is the default, not a semantic model.

| parameter | type | required | default | description |
|---|---|---|---|---|
| `session_id` | string | yes |  | — |
| `db_path` | string |  |  | — |
| `model` | string |  |  | — |
| `dim` | integer |  |  | — |
### `research_semantic_search`

Hybrid vector/FTS5 search over selected passages; semantic retrieval is optional.

| parameter | type | required | default | description |
|---|---|---|---|---|
| `session_id` | string | yes |  | — |
| `query` | string | yes |  | — |
| `limit` | integer |  |  | — |
| `path` | string |  |  | — |
| `db_path` | string |  |  | — |
| `model` | string |  |  | — |
| `alpha` | number |  |  | — |
| `hybrid` | boolean |  |  | — |
### `research_source_register`

Register a book, note, website, article, dataset or other source with format metadata and a default holding.

| parameter | type | required | default | description |
|---|---|---|---|---|
| `session_id` | string | yes |  | — |
| `source_type` | string | yes |  | — |
| `metadata` | object | yes |  | — |
| `root_id` | string |  |  | — |
| `path` | string |  |  | — |
| `url` | string |  |  | — |
| `sha256` | string |  |  | — |
| `reference` |  |  |  | — |
| `holding` |  |  |  | — |
| `reviewer` | string |  |  | — |
### `research_source_types`

List supported source formats and their default epistemic holdings.

| parameter | type | required | default | description |
|---|---|---|---|---|
| `session_id` | string | yes |  | — |
### `research_tei_preview`

Parse a selected TEI/JATS document into sections, references and citation targets.

| parameter | type | required | default | description |
|---|---|---|---|---|
| `session_id` | string | yes |  | — |
| `root_id` | string | yes |  | — |
| `path` | string | yes |  | — |
| `record_references` | boolean |  |  | — |
### `research_theory_compare`

Compare shared/unique evidence, compatibility and discernment between two theories.

| parameter | type | required | default | description |
|---|---|---|---|---|
| `session_id` | string | yes |  | — |
| `theory_a` | string | yes |  | — |
| `theory_b` | string | yes |  | — |
### `research_theory_compatibility`

Record a graded compatibility relation between two theories.

| parameter | type | required | default | description |
|---|---|---|---|---|
| `session_id` | string | yes |  | — |
| `theory_a` | string | yes |  | — |
| `theory_b` | string | yes |  | — |
| `degree` | string | yes |  | — |
| `scope` | string |  |  | — |
| `rationale` | string |  |  | — |
| `reviewer` | string |  |  | — |
| `evidence_ids` | array |  |  | — |
### `research_theory_compatibility_matrix`

Return a deterministic theory compatibility matrix.

| parameter | type | required | default | description |
|---|---|---|---|---|
| `session_id` | string | yes |  | — |
### `research_theory_link_evidence`

Link a theory to supporting, challenging, discriminating or explained records.

| parameter | type | required | default | description |
|---|---|---|---|---|
| `session_id` | string | yes |  | — |
| `theory_id` | string | yes |  | — |
| `target_id` | string | yes |  | — |
| `relation` | string |  |  | — |
| `rationale` | string |  |  | — |
| `reviewer` | string |  |  | — |
### `research_theory_record`

Record a theory with assumptions and predictions.

| parameter | type | required | default | description |
|---|---|---|---|---|
| `session_id` | string | yes |  | — |
| `name` | string | yes |  | — |
| `statement` | string | yes |  | — |
| `scope` | string | yes |  | — |
| `mechanism` | string |  |  | — |
| `assumptions` | array |  |  | — |
| `predictions` | array |  |  | — |
| `assumption_ids` | array |  |  | — |
| `prediction_ids` | array |  |  | — |
| `reviewer` | string |  |  | — |
| `status` | string |  |  | — |
| `notes` | string |  |  | — |
### `research_theory_report`

Return one theory or the full theory ledger with checks.

| parameter | type | required | default | description |
|---|---|---|---|---|
| `session_id` | string | yes |  | — |
| `theory_id` | string |  |  | — |
### `research_theory_rivalry`

Record a rival relation between two theories.

| parameter | type | required | default | description |
|---|---|---|---|---|
| `session_id` | string | yes |  | — |
| `theory_a` | string | yes |  | — |
| `theory_b` | string | yes |  | — |
| `scope` | string |  |  | — |
| `rationale` | string |  |  | — |
| `reviewer` | string |  |  | — |
### `research_theory_scan`

Find incomplete theories, untested predictions and non-discriminating rivalries.

| parameter | type | required | default | description |
|---|---|---|---|---|
| `session_id` | string | yes |  | — |
### `research_trace`

Trace directed research derivations.

| parameter | type | required | default | description |
|---|---|---|---|---|
| `session_id` | string | yes |  | — |
| `node_id` | string | yes |  | — |
| `max_depth` | integer |  |  | — |
### `research_zotero_import`

Read items from the Zotero local API without writing to the library.

| parameter | type | required | default | description |
|---|---|---|---|---|
| `session_id` | string | yes |  | — |
| `base_url` | string |  |  | — |
| `collection` | string |  |  | — |
| `limit` | integer |  |  | — |
| `apply` | boolean |  |  | — |
