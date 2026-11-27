# MCP tools — coding surface

> **Generated file.** Built by `scripts/docs/generate_docs.py` from `src/ozagia/integrations/mcp/server.py`, `src/ozagia/integrations/mcp/graph_surface.py`, `src/ozagia/integrations/mcp/sre.py`, `src/ozagia/research/mcp.py` (ozagia `0.1.5.dev0`). Do not edit by hand: change the source code and regenerate.

98 tools.

> Built-in tools from `server._TOOLS`, plus the offline SRE/IaC tools (`sre.py`) and `research_fetch`.

> Conditionally listed: `dev_rule` (only when the snapshot has Rule nodes), `dev_seed` (only when the snapshot has Seed nodes).

> A project's `[adapters]` configuration can add tools at runtime.

### `dev_architecture`

Deterministic architecture summary: hubs, reference cycles (Tarjan SCC), coupling, module boundaries, dead-code candidates (heuristic) and test/source ratio. File-level and syntactic; use LSP/SCIP for compiler-accurate answers.

| parameter | type | required | default | description |
|---|---|---|---|---|
| `top` | integer |  | 15 | — |
### `dev_assets_list`

List durable external assets recorded in the read-mostly asset catalog (datasets, models, corpora, installs, caches and tools): id, path, kind, owner, scope, source, licence, size, inodes, verification state and pin/deletion flags. Filters owner, kind, state, pinned and limit. Metadata only; no file contents. Read-only: there is no MCP register, pin, verify or delete tool.

| parameter | type | required | default | description |
|---|---|---|---|---|
| `owner` | string |  |  | Only this owner (ozagia, ocelot, huggingface, user, upstream, external). |
| `kind` | string |  |  | Only this kind (dataset, model, corpus, install, cache, tool, other). |
| `state` | string |  |  | Only this state (present, missing, partial, verified, mismatch, unverified). |
| `pinned` | boolean |  |  | Only pinned (true) or unpinned (false) assets. |
| `limit` | integer |  | 100 | Most assets to return (at most 1000). |
### `dev_assets_status`

Durable external asset status: totals by kind, owner and state, allocated/apparent bytes and inodes, pinned/deletable counts and the largest entries. Metadata only and read-only; it never hashes, walks or deletes anything.

| parameter | type | required | default | description |
|---|---|---|---|---|
| `top` | integer |  | 10 | Largest entries to include (at most 100). |
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
### `dev_mesh_inbox`

Read-only metadata rows from this session's local mesh mailbox: id, from, kind, created/expires, read/acked. Subject and body are never returned here; use `ozagia mesh read` for a body. Needs OZAGIA_MESH_HARNESS/OZAGIA_MESH_SESSION and never acks, sends or changes a message.

| parameter | type | required | default | description |
|---|---|---|---|---|
| `unread` | boolean |  | false | Only unread messages. |
### `dev_mesh_status`

Read-only local session-mesh status for this repository: mesh id, session counts (live/stale), peers, links/derived roles, active and queued leases, delegations and unread counts. Self is resolved from OZAGIA_MESH_HARNESS/OZAGIA_MESH_SESSION when set. Metadata only: no message bodies or subjects, and it never sends, leases, starts a daemon or changes state.

| parameter | type | required | default | description |
|---|---|---|---|---|
| `include_all` | boolean |  | false | Also list ended, dead and foreign sessions in peers. |
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
### `dev_overview`

One stable reference page for the repository: identity and package metadata, top-level layout, entry points, module responsibilities (from module docstrings), documentation map, architecture (when a tag cache exists), test-to-source links, commands/conventions and freshness. Deterministic and read-only; it never imports or runs the target. Use it when you need 'what is this repo and where do I start' without reading many files.

| parameter | type | required | default | description |
|---|---|---|---|---|
| `format` | string |  | "markdown" | — |
| `budget_tokens` | integer |  | 6000 | Bound the Markdown response. |
| `max_modules` | integer |  | 40 | Maximum modules listed. |
### `dev_ozma_flight`

Read-only declared Ozma flight view for this repository: flight id/source, bounded members (harness/session/key, host-provided agent id, role and workunit derived only from that id, scope, verification, transport, wake modes, state), role/workunit counts, advisory claim/blocker summaries, handoff counts and host-approved wake counts. Metadata only: no message bodies, criteria text, credentials or card files. Conformance remains declared and unverified; there is no send, claim, wake approval, handoff accept, merge or network action.

| parameter | type | required | default | description |
|---|---|---|---|---|
| `limit` | integer |  | 32 | Most members to list (at most 64). |
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
### `dev_resource_grants`

Read-only resource-grant declaration and claim-ledger view: declared grants with their kind, provider, declared permissions, budget, cleanup and approval requirement, plus claim counts (active, released, expired, orphaned) and bounded claim rows. It never claims, approves or releases a grant, never starts a daemon, makes no network call and returns no credential value; expiry and cleanup are reported, not changed.

| parameter | type | required | default | description |
|---|---|---|---|---|
| `grant_id` | string |  |  | Only claims for this grant id. |
| `claim_status` | string |  |  | Only claims in this state. |
| `limit` | integer |  | 50 | Maximum grants and claims shown (max 200). |
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
### `dev_tmp_list`

List managed temporary-space entries: id, path, kind, session, reason, TTL/expiry, state, bytes, files and tmpfs membership. Metadata only, no file contents or prompts. Read-only: creation is the OpenCode ozagia_scratch tool, the harness or the CLI; no MCP tool creates or deletes an entry.

| parameter | type | required | default | description |
|---|---|---|---|---|
| `kind` | string |  |  | Only this kind (scratch, agent, check, eval, engine, cache, other). |
| `session` | string |  |  | Only entries of this harness session id. |
| `owned` | boolean |  |  | Only owned (true) or unowned (false) entries. |
| `include_all` | boolean |  | false | Include released/expired entries too. |
| `limit` | integer |  | 100 | Most entries to return (at most 500). |
### `dev_tmp_status`

Managed temporary-space status: totals by kind, session and ownership, filesystem capacity and warnings, and registry errors. Metadata only and read-only; there is no MCP create, write or delete tool.

Input schema: `{"type": "object", "properties": {}, "required": [], "additionalProperties": false}`

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

Read-only static checks for declared cross-repository API and code-to-infrastructure relationships, explicit API mappings with contract-first references, declared package dependency links, and declared or package-derived repository relationships. Designated references may be fetched and fast-forwarded over Git; set no_sync to opt out. Indexes are not refreshed; missing evidence remains unknown.

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

Read-only search over discovered or explicitly selected repository members. The default graph mode searches existing snapshots; code mode is opt-in and searches current member source, writes only its cache outside member checkouts, does not refresh indexes, and is relevance-ranked only. Designated references may be fetched and fast-forwarded over Git; set no_sync to opt out. Results retain provenance. In graph mode, set family_boost true to prefer hits from members sharing a declared anchor family; it is opt-in and defaults to false. Code mode rejects family_boost.

| parameter | type | required | default | description |
|---|---|---|---|---|
| `query` | string | yes |  | — |
| `roots` | array |  |  | Optional explicit member roots. |
| `limit` | integer |  | 20 | — |
| `mode` | string |  | "graph" | graph reads member snapshots; code reads current member source and caches outside member checkouts (opt-in, relevance-only). |
| `family_boost` | boolean |  | false | Graph mode only: prefer hits from members sharing a declared anchor family (opt-in; default off). Code mode rejects true. |
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
### `research_fetch`

Fetch the text of a paper whose web fetch failed (404/403, a PDF the fetch tool can't read, a publisher landing page). Resolves open-access copies (arXiv, DOI via Crossref/OpenAlex/Unpaywall, PubMed/PMC via Europe PMC, Semantic Scholar, OpenReview, citation_pdf_url on a landing page), downloads with a size cap, extracts PDF or JATS text, caches it by content hash and returns it page by page with a stable source: key. Pass offset=next_offset for the next part, refresh=true to resolve again instead of using the cache. Open access only; network is off with OZAGIA_RESEARCH_OFFLINE=1 (cached copies are still served).

| parameter | type | required | default | description |
|---|---|---|---|---|
| `url` | string | yes |  | The URL that failed. |
| `max_chars` | integer |  |  | Characters to return (default 20000, at most 100000). |
| `offset` | integer |  |  | Character offset into the document text (default 0). |
| `refresh` | boolean |  |  | Resolve the URL again instead of answering from the cache (default false). |
