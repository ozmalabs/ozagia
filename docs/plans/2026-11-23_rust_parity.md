# Rust parity with Python — plan (2026-11-23)

Status: Phases 0, 0.5, 1 and 2 implemented (2026-11-23); Phases 3–7 proposed. This plan closes
the gap between `.rs` and `.py` support across the graph, tags,
symbol-question layer, edits, LSP, tests and documentation, and adds the
initial setup for a Python→Rust migration assistant built on the same graph.
It respects the repository rules: the core stays standard-library only,
tree-sitter and language servers stay optional extras, and no target project is
imported or executed implicitly.

## Objective

After this work, an agent working in a Rust repository can do everything it can
do in a Python repository today, at the same honesty level:

- the graph knows Rust symbols, spans, docs, modules, `use` imports and tests;
- `repo map`, `outline`, `callers`, `callees`, `impact` and `_structure`
  resolve Rust symbols across files without false confidence;
- `dev_definition`, `dev_references`, `dev_diagnostics`, `dev_callers`,
  `dev_callees`, `dev_rename_preview` answer through rust-analyzer when it is
  installed, and say why when it is not;
- symbol-addressed edits (`replace_symbol`, `replace_body`, `insert_import`,
  `delete_symbol`, `rename_symbol`, …) work on `.rs` with the same transaction
  contract as `.py`;
- a captured `cargo test` gets the same opt-in affected selection and result
  cache as a captured `pytest`;
- a Python→Rust port can be inventoried, paired, reported and handed off as
  tasks, with manual and unsupported constructs flagged instead of guessed;
- coverage and limits are documented per capability rather than implied.

## Current state (verified 2026-11-23)

| Layer | Python | Rust today |
|---|---|---|
| Tag extraction | stdlib AST: defs, refs, scoped imports, qualnames, end lines | bundled `index/queries/rust-tags.scm`: defs/refs with impl scopes (`Foo.bar`), trait signatures, const/static/enum variants, qualified calls, and `use` import records; Phase 1 |
| Graph symbols | `_python_parse`: qualnames, end lines, docstrings, decorators, args, returns, bases, imports, tests | `rust_symbols.parse_rust`: tree-sitter fields (qualname, spans, doc, attributes, args, returns, visibility, test marker) with regex fallback; Phase 0 `mod` resolution retained; `#[path]` recorded as unsupported |
| Test indexing | `index_test` parses pytest tests, links `tests_module`, `mentions_file` | Phase 1 adds `#[test]`/`#[tokio::test]`/`#[rstest]` and `tests/*.rs` Test nodes; import/`tests_module` resolution remains Phase 2/5 |
| Repo map | file edges from resolved imports + names | file edges from resolved Rust `use`/glob module targets plus name edges; `mod` edges resolve |
| Symbol graph | local / import / name resolution; pyright precise mode | Phase 2 Rust resolver covers `crate`/`self`/`super`/workspace crates, aliases, globs and bounded re-exports; external crates and ambiguous bindings stay unresolved; Python resolution unchanged |
| Edits | symbol ops (`replace_symbol`, `replace_body`, `insert_import`, `delete_symbol`, `rename_symbol`, …) with AST parse gate | CST subtree ops only (`edit/cst.py`); no symbol addressing, no `use` insertion, no symbol rename |
| LSP | pyright live | registry entry exists; Phase 0 installed rust-analyzer 1.97.1, but the first query can be empty while it indexes a workspace |
| Lint | ruff | rustfmt (already parity for formatting; no Rust linter like clippy) |
| Tests | `pytest` capture, affected subset, result cache | `cargo test` is not a captured command; no crate or test-target nodes |
| Dependencies/review | Python manifests + trust-boundary dataflow | Cargo.lock inventory, crates.io, purls, strict SemVer advisories; no Rust dataflow |

Cross-language migration has no support at all today: there is no Python→Rust
pairing, status, parity report or hint surface, and the graph has no
`migrates_to` relationship. This plan adds the read-only foundation for it
(Phase 6) without turning ozagia into a transpiler.

Language support packages are new as of Phase 0.5: each language has a
`lang-*` extra and a module that declares its parser, language server and
linter, and CLI paths report/install missing safe components when they
encounter the language (see [`languages.md`](../languages.md)).

The tag backend itself is healthy: `backend_status("rust")` reports
`definitions: true`, `references: true`, and a sample file yields struct,
method, trait, module, function and call tags. The gap is depth, not
availability of a parser.

## Design decisions

1. **Tree-sitter is the structural parser; rust-analyzer is the semantic one.**
   Indexing, tags, outlines and symbol edits must work with `tree-sitter-rust`
   alone, exactly as Python works without pyright. rust-analyzer is an optional
   extra for precise references, diagnostics and rename preview, never a
   requirement for a snapshot. A missing grammar falls back to the current
   regex parser and reports that it did.
2. **One parser, one truth.** Add `src/ozagia/index/rust_symbols.py` as the
   Rust source model (symbols, modules, `use`, tests, spans, docs). The graph,
   tag cache, edit adapter and test mapping all consume it; keep the regex
   parser only as the no-grammar fallback.
3. **Cargo metadata is read, never built.** Use stdlib `tomllib` to read
   `Cargo.toml`, workspace members and package names. `cargo metadata` would
   execute build logic, so it is refused unless a future explicit-trust
   setting says otherwise.
4. **Imports are tag records.** `use` statements become `kind: "import"` tags in
   `.ozagia/tags.json` (like Python's `import.from` records) so the symbol
   graph keeps its single cache-driven input and repo-map edges gain Rust
   imports without a second index.
5. **Symbol edits are a separate adapter.** `src/ozagia/edit/rust_edit.py`
   mirrors the Python op names and refusal semantics, reuses `edit/txn.py`, and
   adds a tree-sitter parse gate (no `ERROR`/missing nodes). `edit/cst.py`
   remains the lossless subtree adapter for `.rs`/`.lua`.
6. **Execution stays opt-in.** Reading Cargo metadata is safe; running `cargo`
   is not (build scripts and proc macros execute target code). `cargo test`
   support is limited to a command the caller typed, and a future clippy lint
   is an explicit-trust setting. No implicit build, check or fetch.
7. **Precise offline graph is optional.** `rust-analyzer scip .` +
   `scip print --json` can feed the existing `index/scip.py` importer. This is a
   phase-7 enhancement, not a dependency of the syntactic graph.
8. **Honest degradation is part of done.** Every new query reports
   `available`, `backend`, `error` and coverage notes; unresolved external
   crates and `#[path]`/`include!`/generated modules are reported, not guessed.
9. **Migration is an overlay, not a transpiler.** Pairing, status and hints are
   derived statically from the Python and Rust inventories plus explicit config.
   Reports and task checklists live under `.ozagia/migration/`; inferred pairs
   are candidates and never become status on their own. Scaffold previews go
   through `edit/txn.py` and write only on an explicit request. Graph
   `migrates_to` edges are deferred until the overlay proves useful, so the
   snapshot format and build path stay stable.
10. **Languages are support packages.** Every language has one module and one
    `lang-*` extra declaring its parser, language server, linter and manual
    components. Encountering the language in a CLI build/index/agent path
    reports missing components and installs the safe ones (pip specs and
    rustup components) with a per-language message. It never uses sudo, npm or
    an OS package manager automatically. `OZAGIA_LANGUAGE_AUTOINSTALL=0`
    disables the automatic path; `ozagia languages detect|ensure` is the
    explicit surface. This is separate from `ozagia-toolchain`, which remains
    discovery-only.

## Phases

### Phase 0 — Unblock and correct the baseline (0.5–1 day) — implemented 2026-11-23

- Installed the rust-analyzer rustup component in the dev toolchain (1.97.1)
  and documented `rustup component add rust-analyzer`; no code installs a
  toolchain. `lsp.server_status()` reports availability and
  `last_start_error`.
- Fixed `mod` resolution in `core/memory.py`: `mod foo;` resolves to `foo.rs`
  **or** `foo/mod.rs`; crate roots (`lib.rs`, `main.rs`, `build.rs`,
  `src/bin/*.rs`, test/example/bench roots) resolve beside themselves; a
  module declared inside `foo.rs`/`foo/mod.rs` resolves under `foo/`;
  `#[path = "..."]` is recorded in the file's `rust_unsupported_mods` attribute
  and produces no edge. Reingest records both layouts and follows a module
  moving between them.
- Refreshed stale docs: `docs/tag_languages.md` output shape, the Rust/Lua
  edit-adapter contradiction in `docs/memory_mcp.md`, the rust-analyzer install
  status in `docs/lsp.md`, and new `docs/rust.md` with the current support
  matrix and module heuristics.
- Verification: `tests/test_rust_mods.py` (4 tests covering crate roots,
  `mod.rs`, `#[path]` and a reingest layout switch); `test_graph_reingest.py`,
  `test_memory.py` and `test_repo_map.py` all pass (90 passed). The live LSP
  suite starts rust-analyzer 1.97.1; its first definition can still be empty
  while indexing, which Phase 4 addresses with a bounded warm-up.

### Phase 0.5 — Per-language support packages (1–2 days) — implemented 2026-11-23

- Added one support package per language under `src/ozagia/languages/`
  (Python, Rust, Lua, TypeScript/JavaScript, Go, Java, C, C++, Kotlin, Swift),
  each exporting a `LanguageSupport` with suffixes, components, probes,
  managers, required/optional flags and a `lang-*` extra.
- Added the `lang-python`, `lang-rust`, `lang-lua`, `lang-typescript`,
  `lang-go`, `lang-java`, `lang-c`, `lang-cpp`, `lang-kotlin` and `lang-swift`
  extras to `pyproject.toml`; the existing `treesitter` extra stays as the
  union.
- `detect` scans for suffixes (bounded, skip-aware); `ensure` reports and
  installs missing safe components. The user-visible message is
  `[ozagia] languages: installing required components for <Language>: ...`.
  Only uv-pip (pip fallback) specs declared in the support modules and
  `rustup component add` run automatically; system, npm and unknown components
  are reported with their manual command and never installed.
- Wired the check into `ozagia index`, `ozagia agent`, the first
  `ozagia replace` build and `ozagia-memory build`; `--no-language-install`
  and `OZAGIA_LANGUAGE_AUTOINSTALL=0` disable it. A failed check or install is
  reported and never blocks the command.
- Added `ozagia languages list|detect|ensure` and the `ozagia-languages`
  script; `docs/languages.md` documents packages, safety and limits.
- Verification: `tests/test_languages.py` covers registry coverage, detection,
  install reporting, no-install, network blocking, manual/system handling and
  uv-first/pip-fallback command construction (10 passed).

### Phase 1 — Structural parser and tags (2–3 days) — implemented 2026-11-23

- Added `src/ozagia/index/rust_symbols.py`:
  - `parse_rust(path, source)` returns `symbols`, `imports`, `tests`,
    `modules`, `source`, `backend`, `has_errors`, `error` and `crate_root`.
  - Symbols: functions/methods, structs, enums, unions, traits, type aliases,
    impl blocks, modules, consts, statics and macros; qualnames include the
    impl/trait type (`Foo.bar`, `Trait.method`); spans, outer doc comments,
    attributes as `decorators`, parameter names, return type and visibility.
  - `#[test]`, `#[tokio::test]`, `#[rstest]`, `#[quickcheck]`, `#[proptest]`
    and `#[test_case]` are recorded; inline tests and `tests/*.rs` become Test
    nodes. `#[cfg(test)]` marks an inline test-module context.
  - `use` extraction covers single paths, groups, aliases, globs, `self`,
    `crate`/`super` prefixes and `pub use`, retaining the enclosing scope.
  - Tree-sitter is optional and imported lazily; the regex fallback returns
    shallow symbols, modules, imports and test markers with a visible backend.
- Added `src/ozagia/index/queries/rust-tags.scm`, a bundled superset of the
  upstream query: existing definitions/references plus const/static, enum
  variants, trait signature methods, qualified calls and an `@scope.impl`
  capture. Methods now nest as `Type.method` without a duplicate `impl` def.
- Wired `.rs` to the bundled query (`query_package: "ozagia"`), bumped
  `TAGS_SCHEMA_VERSION` and `repo_map.CACHE_VERSION` (now 5 after Phase 2's
  qualified-path field); extractor fingerprints invalidate existing Rust tag
  caches.
- `core/memory.py` delegates `.rs` source and test indexing to `rust_symbols`:
  Symbol nodes carry the Python-parity fields, impl methods get
  `contains_symbol` edges, inline/source tests and `tests/*.rs` get Test nodes,
  and external `mod` edges keep the Phase 0 resolution. `use` records live in
  the tag cache for Phase 2 resolution.
- Verification: `tests/test_rust_symbols.py` (5 tests) covers CST fields,
  qualnames, docs/attributes, args/returns, use forms, modules, regex fallback
  and graph integration; `tests/test_tag_languages.py`, `test_rust_mods.py`,
  `test_repo_map.py`, `test_memory.py` and `test_graph_reingest.py` pass, and
  the full suite is 6230 passed / 22 skipped. Python tag output is unchanged.

### Phase 2 — Rust imports, modules and symbol-graph parity (3–5 days) — implemented 2026-11-23

- Added `src/ozagia/core/rust_modules.py`: reads `Cargo.toml` with stdlib
  `tomllib` (no `cargo metadata`, no build) and builds library, binary,
  test/bench targets and their module trees. It handles `src/lib.rs`,
  `src/main.rs`, `src/bin/*.rs`, configured `[lib]`/`[[bin]]`/`[[test]]`/
  `[[bench]]` paths, workspace member manifests, inline modules, `foo.rs` /
  `foo/mod.rs`, nested module directories and `#[path]`.
- `use` resolution covers `crate::`, `self::`, `super::`, leading `::`,
  workspace crate names, aliases, groups, globs and `pub use` re-exports for
  up to three hops. Relative child-module paths (`inner::go`) resolve; a
  type/method path such as `Foo::bar` is left to local scope resolution.
  External crates are reported external and never guessed by name; ambiguous
  modules/missing files are unresolved and reported.
- Tag records carry `{kind: "import", detail: "import.use", module, imported,
  alias, glob, pub, scope, line, path}`; Rust qualified calls also retain their
  full `path` (`crate::a::b`, `Foo::bar`). `TAGS_SCHEMA_VERSION` and
  `repo_map.CACHE_VERSION` moved to 5.
- `core/symbol_graph.py` resolves Rust references before the shared Python
  local/name fallback. Exact import targets become `resolution: "import"`
  edges at confidence 1.0; a file with an external/unresolved binding or glob
  suppresses bare-name guessing for that reference. Stats add
  `rust_module_targets`, `resolved_import` and `unresolved`.
- `repo_map.build_reference_graph` adds resolved Rust import/glob file edges,
  so `use` contributes to PageRank. The Python resolver and `_PY_ROOTS`
  behavior are unchanged.
- Verification: `tests/test_rust_modules.py` (5 tests) covers workspace targets,
  module trees, inline modules, `crate`/`self`/`super`, aliases, globs,
  cross-crate imports, re-exports, external false-edge suppression, symbol
  graph and repo-map edges. The full suite is 6236 passed / 22 skipped; the
  focused Rust/import/graph suites pass, and ruff is clean.
- Precise-mode rust-analyzer warm-up and complete live coverage remain Phase 4;
  the existing precise path still uses the LSP client.

### Phase 3 — Symbol-addressed Rust edits (3–4 days)

- New `src/ozagia/edit/rust_edit.py` with the Python op vocabulary where it
  maps:
  - `replace_symbol`, `replace_body`, `delete_symbol`,
    `insert_before_symbol`, `insert_after_symbol`;
  - `insert_import` (inserts a `use`, merging into an existing group when
    unambiguous, sorted by rustfmt conventions);
  - `replace_span` (already available through CST; expose the same op here);
  - `rename_symbol` for the **same file only**, scope-aware through
    tree-sitter scopes; refuse fields, macros, ambiguous bindings and
    trait/impl method renames that could affect other types.
- Reuse `edit/txn.py` unchanged: preview by default, `base_sha256` check,
  atomic write, backup, journal. Add a parse gate: the edited source must parse
  with `tree_sitter_rust` without `ERROR`/missing nodes; refuse rather than
  write.
- `edit/dispatch.py`: `.rs` dispatches symbol ops to `rust_edit` and CST ops to
  `cst`; unknown ops are refused. `code_edit.py` stays Python-only.
- Rename application from `lsp.rename_preview`: translate a same-file preview
  into `replace_span` edits through the transaction (the Python path does the
  same today); cross-file application remains preview-only.
- Exit: every Python symbol op has a Rust counterpart or an explicit refusal;
  docs/tests show preview, stale-base, parse-gate and lossless behaviour.

### Phase 4 — LSP parity (1–2 days)

- Toolchain/status: `ozagia-toolchain` and `lsp.server_status()` explain the
  rustup component state and the exact install command; `available` continues
  to mean "binary found", not "protocol works".
- Live tests: a minimal Cargo fixture exercises definition, references,
  document symbols, diagnostics, call hierarchy and rename preview under
  `OZAGIA_LIVE_LSP=1`; skip when the component or a Cargo project is absent.
- Root selection: use the workspace root (`Cargo.toml`) rather than the file's
  directory; keep one shared client per root/language.
- Precise mode integration: `symbol_graph` uses LSP confirmation for Rust the
  same way it does for Python; expose `precision` facts and fall back silently
  to syntactic rows when unavailable.
- Exit: live Rust LSP suite passes when the component is installed; all
  unavailable paths are honest and tested.

### Phase 5 — `cargo test` parity (5–8 days)

- **Command capture**: `intent/shell.py` recognizes `cargo test` (workspace,
  `-p/--package`, `--test`, `--lib`, `--bins`, `--examples`, `--benches`,
  `--all-targets`, feature flags, filters, `--` args, test-thread/env options)
  and takes over only forms whose semantics are fully understood; ambiguous
  forms run as typed.
- **Runner**: `intent/run.py` gets `run_cargo_test` with the same bounded
  timeout, output tail and `[ozagia] cargo test ...` head-line contract. Parse
  the stable human output (`running N tests`, `test ... ok/ignored/FAILED`,
  `test result:`), not nightly JSON. Failures are never cached.
- **Rust test graph**: test functions from `#[test]`/`#[cfg(test)]`, plus
  integration tests under `tests/`, benches and examples; map each test target
  to its crate and file; add `has_test`, `tested_by_file`, `tests_module` and
  `mentions_file` links using the Phase 1/2 symbol and import model.
- **Affected selection**: reuse `[tests] select` and `cache`. Key a green run
  on the cargo/rustc versions, toolchain, workspace manifests, `Cargo.lock`,
  feature flags and environment; map changed files to crates/targets and run
  `cargo test -p <crate> --test <target> <filter>` only when narrow. Run
  everything as typed when a build script, proc-macro crate, workspace
  manifest, non-Rust file or unlinked crate changed, or planning is ambiguous.
  `OZAGIA_TESTS_ALL=1` keeps its meaning.
- **Security**: tests execute project code, so nothing runs unless the caller
  typed `cargo test`; no implicit `cargo check`/`build`/`clippy`.
- Exit: a fixture with two crates and three test targets runs only the linked
  targets after a one-file change; every run-all reason is printed; cache and
  counters match the pytest contract.

### Phase 6 — Python→Rust migration assistant, initial setup (3–5 days)

The goal is a graph-aware migration cockpit, not a source-to-source compiler:
know what has a counterpart, what is missing, what diverged, and what to do
next, and flag everything that needs a human or a different design.

- **Configuration** (`config.py`, `.ozagia.toml`): a validated `[migration]`
  section with source/target trees, optional inference, explicit pairs and
  status overrides.

  ```toml
  [migration]
  python = "python"
  rust = "crates"
  infer = true

  [[migration.pairs]]
  python = "pkg.models.User"
  rust = "core::model::User"
  status = "in_progress"   # optional manual override
  ```

  Reject roots outside the repository, duplicate pairs, unknown statuses and
  paths that do not exist; expose the effective config through the settings
  and status surfaces.
- **Inventories** (`src/ozagia/migration/inventory.py`): Python side reuses
  `_python_parse`/tags (public names, `__all__`, signatures, decorators, async,
  doc first line); Rust side reuses `rust_symbols` (Phase 1) (`pub` items,
  paths, signatures, docs, impl/trait ownership). Both are bounded to the
  configured trees and never execute the project.
- **Matcher** (`src/ozagia/migration/match.py`): explicit pairs win;
  `infer = true` adds name/path/snake-case candidates with a confidence, which
  are reported as candidates and never auto-accepted. Status vocabulary:
  `planned`, `in_progress`, `ported`, `diverged`, `dropped`, `extra`, with
  derived parity flags (missing counterpart, arity, async, visibility, doc or
  signature mismatch) and manual-override precedence.
- **Hints** (`src/ozagia/migration/hints.py`): a deterministic, documented
  construct/type table: class→struct/enum, dataclass→struct + derives,
  `Protocol`→trait, `Enum`→enum, exceptions→`Result`/error enum,
  `async def`→`async fn`, `Optional`/`Union`→`Option`/enum (manual),
  `list`/`dict`/`set`/`tuple`→`Vec`/`HashMap`/`HashSet`/tuple,
  `str`→`String` or `&str` (choice flagged), `Callable`, `Any`,
  metaprogramming, monkeypatching and C extensions marked manual or
  unsupported with a reason. No hint pretends to be compilable code.
- **Report, CLI and MCP**: `ozagia migration status [--json|--markdown]` and a
  bounded `dev_migration` tool return counts per tree, per-status totals, the
  top gaps and divergences, freshness relative to both trees, and explicit
  omissions. Reports are written under `.ozagia/migration/`; the source tree
  is untouched.
- **Task hand-off**: `ozagia migration tasks` emits one checklist item per
  unported or diverged symbol with success criteria that fit the existing task
  and evidence vocabulary (counterpart exists, parity flags clear, mapped
  tests pass). It does not create goals or run anything without the caller.
- **Scaffold preview (bounded)**: `ozagia migration scaffold <python-symbol>
  --target <rust-file>` renders a suggested Rust skeleton from the hints and
  runs it through `edit/txn.py` as a preview. `--write` requires the normal
  explicit edit permission; complex classes, unions, decorators and unknown
  types are refused with reasons. Simple dataclass/function shapes only.
- **Docs**: `docs/migration.md` — config schema, status vocabulary, type and
  construct table, unsupported list, freshness and trust limits.
- **Tests**: fixture `tests/fixtures/py_rust_migration/` with a Python package
  (dataclass, protocol, async function, union, `__all__`) and a partially
  ported Rust crate; `tests/test_migration_config.py`,
  `test_migration_inventory.py`, `test_migration_match.py`,
  `test_migration_report.py`, `test_migration_cli.py`, plus an MCP tool test.
- Exit: the fixture reports ported, planned, diverged and extra items
  correctly; hints mark manual/unsupported constructs; no write happens
  without an explicit scaffold `--write`; docs and CLI output are honest about
  stale snapshots and inference confidence.

### Phase 7 — Optional precise graph and review (2–4 days, stretch)

- SCIP recipe: document `rust-analyzer scip .` and
  `scip print --json index.scip`, ingest through `index/scip.py`, and add a
  fixture round-trip test. Optionally cache the normalized index under
  `.ozagia/`.
- Measure on a real Rust repository with `ozagia.eval.bench` (cold/warm index,
  symbol/import resolution counts) and record results under `docs/results/`.
- Declare the Rust tier in `docs/rust.md`, `docs/status.md` and the
  capabilities/self-graph audit (`tags`, `symbols`, `imports`, `edits`, `lsp`,
  `tests`, each with an explicit limit).
- Deep review: keep Cargo.lock/advisory support. A Rust trust-boundary/dataflow
  analyzer is **not** part of this plan; it is a new analyzer with the same
  scope as `core/trust_boundary.py`, and deserves its own plan.

## Parity matrix at completion

| Capability | Done when |
|---|---|
| Tags | Definitions, references and `use` imports in `.ozagia/tags.json` with qualnames; const/static/trait methods/macros covered; backend status reports gaps |
| Graph | Symbol nodes have the Python field set; impl methods are `Type.method`; test functions are Test nodes; `mod`/`use` edges resolve |
| Repo map / outline | Rust imports contribute PageRank edges; outlines show qualified definitions and spans |
| Symbol graph | Local/import/name resolution with confidence; external crates unresolved; per-language stats |
| Edits | Symbol ops + CST ops; preview-first transaction; tree-sitter parse gate; same-file rename |
| LSP | Works when rust-analyzer is installed; honest status when not; live suite gated |
| Lint | rustfmt unchanged; optional clippy only behind an explicit trust setting (not in the default path) |
| Tests | Captured `cargo test`; affected subset and cache; conservative run-all fallbacks |
| Migration | `.ozagia.toml` pairs validate; `ozagia migration status` reports planned/ported/diverged/extra from both inventories with confidence and freshness; scaffold is preview-only unless `--write` |
| Language support | every tag-registry language has a `lang-*` extra and module; `ozagia languages detect/ensure` reports and installs missing safe components with `installing required components for <Language>`; system/npm components are reported only |
| Docs | `docs/rust.md`, `docs/languages.md`, `docs/migration.md`, refreshed tag/LSP/edit docs, this plan marked implemented/partial |

## Test plan

New and extended suites:

- `tests/test_rust_symbols.py` — parser fields, qualnames, impl/trait methods,
  docs/attributes, consts/statics/macros, tests, use forms, regex fallback and
  graph Test/containment integration.
- `tests/test_tag_languages.py` — bundled Rust query captures, impl scopes,
  trait signatures, constants, qualified calls and cache version.
- `tests/test_rust_modules.py` — workspace targets, module trees, inline
  modules, `crate`/`self`/`super`, aliases, globs, cross-crate imports,
  re-exports, external false-edge suppression and repo-map edges.
- `tests/test_symbol_graph.py` — Python resolution unchanged; Rust cross-file
  cases live in `test_rust_modules.py`.
- `tests/test_rust_edit.py` — every op, preview/apply, stale hash, parse gate,
  lossless output, rename refusals.
- `tests/test_lsp.py` — fake-server cases unchanged; gated live Rust cases.
- `tests/test_cargo_test.py` — command mapping, output parsing, selection,
  cache keys, run-all reasons (use a fake `cargo` executable for determinism).
- `tests/test_repo_map.py` — Rust import edges and tag-cache migration.
- `tests/test_rust_mods.py` — crate-root and `mod.rs` resolution, `#[path]`
  recording, and a reingest switch between `foo.rs` and `foo/mod.rs`.
- `tests/test_languages.py` — support-package registry coverage, language
  detection, install reporting, no-install/network-blocked paths and
  manual/system components.
- `tests/test_migration_config.py` — schema, root safety, duplicate/unknown
  pairs, settings/status exposure.
- `tests/test_migration_inventory.py` — Python and Rust public inventories,
  `pub`/`__all__`, signatures, docs, unsupported shapes.
- `tests/test_migration_match.py` — explicit pairs, inference confidence,
  status derivation, parity flags and manual overrides.
- `tests/test_migration_report.py` — JSON/Markdown counts, freshness, top gaps,
  no source writes.
- `tests/test_migration_cli.py` — `status`/`tasks`/`scaffold` preview, refusal
  paths and explicit `--write` through the transaction.
- MCP `dev_migration` bounded-output and unavailable-state tests.

Fixtures: `tests/fixtures/rust_project/` — a two-crate workspace with
`src/lib.rs`, `src/mod.rs`-style and 2018-style modules, a trait and impl,
const/static, docs, `use crate/super/self`, an external-crate `use`, unit
tests, `tests/integration.rs` and a second crate used by the first.
`tests/fixtures/py_rust_migration/` — a Python package (dataclass, protocol,
async function, union, `__all__`) and a partially ported Rust crate with
ported, planned, diverged and extra symbols.

Commands:

```bash
.venv/bin/python -m pytest -q \
  tests/test_rust_modules.py tests/test_rust_symbols.py tests/test_rust_mods.py \
  tests/test_symbol_graph.py tests/test_tag_languages.py tests/test_repo_map.py \
  tests/test_lsp.py tests/test_migration_config.py tests/test_migration_inventory.py \
  tests/test_migration_match.py tests/test_migration_report.py \
  tests/test_migration_cli.py
.venv/bin/python -m pytest -q          # full gate
.venv/bin/ruff check src tests
OZAGIA_LIVE_LSP=1 .venv/bin/python -m pytest -q tests/test_lsp.py
python -m ozagia.eval.bench --root <rust-repo> --cold --check
```

## Risks and mitigations

- **Tree-sitter node-name drift.** Pin minimum grammar versions, compile the
  bundled query at load time, and let `backend_status()` report a mismatch as
  an error rather than emitting wrong tags.
- **Workspace resolution edge cases.** Conservative resolution with explicit
  unresolved counts; no name guessing for external crates; a fixture that
  covers workspace members, aliases and globs.
- **Cargo/test output variance.** Parse only stable human lines; if the output
  is unrecognised, report the run honestly and never cache it. No nightly JSON.
- **Execution and supply-chain risk.** Only typed commands run; no implicit
  cargo build. Clippy, if added, requires `[lint] trust_project_rust` and the
  same find-executable policy as ruff.
- **Cache churn.** One deliberate `CACHE_VERSION` bump in Phase 1; later
  extractor changes are covered by `extractor_fingerprint`.
- **False migration confidence.** Inferred pairs stay candidates; explicit
  config and manual status override them; parity flags are facts, not a
  promise of equivalent behaviour. The fixture includes deliberately
  misleading names to test that inference cannot mark a pair `ported`.
- **Migration drift.** Status is keyed to the content of both trees and the
  config; a changed side marks the report stale instead of silently keeping an
  old status. Cross-language `migrates_to` graph edges are deferred so the
  overlay cannot destabilise the snapshot.
- **Scope creep.** Phases 1–3 deliver the coding-agent core; Phase 5 is
  separate because it introduces process execution; Phase 6 is a read-only
  overlay plus preview; Phase 7 is optional. Rust dataflow review is
  explicitly a separate plan.

## Estimated effort

| Phase | Days |
|---|---|
| 0 — baseline/docs (done 2026-11-23) | 0.5–1 |
| 0.5 — language support packages (done 2026-11-23) | 1–2 |
| 1 — parser + tags + graph fields (done 2026-11-23) | 2–3 |
| 2 — modules/imports/symbol graph (done 2026-11-23) | 3–5 |
| 3 — symbol edits | 3–4 |
| 4 — LSP | 1–2 |
| 5 — cargo test | 5–8 |
| 6 — migration assistant (initial setup) | 3–5 |
| 7 — optional SCIP/review/docs | 2–4 |

Core parity (0–5) is roughly 15–23 developer-days, plus the 1–2 days already
spent on the language support packages (Phase 0.5). With the initial migration
assistant the plan is about 18–28 days; Phase 7 is another 2–4. Phases 1 and 2
are the critical path; Phase 5 can start once Phase 1 lands the test nodes, and
Phase 6 needs Phases 1–2 for the Rust inventory.

## Non-goals

- Type inference, trait resolution, macro expansion, MIR/borrow analysis:
  rust-analyzer or SCIP are the precise layers.
- Automatic cross-file rename application; preview plus same-file application
  is the Python-equivalent scope.
- Running `cargo build`, `cargo check` or `cargo clippy` implicitly, or
  importing/building the target project.
- `#[path]`, `include!`, generated source, procedural-macro expansion: reported
  as unsupported coverage gaps.
- Cargo.toml/Toml editing; source edits only.
- Rust trust-boundary/dataflow analysis in this plan (separate analyzer).
- Automatic Python→Rust transpilation, complete crate generation, or claims of
  behavioural equivalence. The migration assistant inventories, pairs, reports
  and hints; scaffold output is a preview and simple shapes are refused when
  the mapping is not safe.
- Running both implementations automatically to compare behaviour, rewriting
  the source tree during status, or creating goals/tasks without the caller.
- Dependency, build, async-runtime or architecture migration planning beyond
  the static hint table; those need project-specific decisions.
- Installing system packages, global npm packages, or anything that needs sudo
  from the language support path. Those are reported with manual commands.
- Isolating each language's dependencies into its own environment; the support
  packages install into the active environment (or user-scoped rustup).

## Open questions (defaults chosen)

1. Is rust-analyzer acceptable as an optional semantic dependency, like
   pyright? **Default: yes, optional and documented, never required.**
2. Should `cargo test` selection be in scope, given it executes project code?
   **Default: yes, only for a typed command and off by default like pytest.**
3. Is clippy wanted as a Rust linter? **Default: later, opt-in behind an
   explicit trust setting; rustfmt remains the default.**
4. Is Rust deep-review dataflow required for "parity"? **Default: no; it is a
   separate analyzer plan and not a wiring task.**
5. How automated should migration assistance be at the start? **Default:
   inventory, pairing, status, hints and task checklists; scaffold preview only
   for simple shapes, no automatic transpilation or behaviour-equivalence
   claims.**
6. Should language components install automatically when a language is
   encountered? **Default: yes on typed CLI paths (index, agent, replace,
   memory build), for pip specs and rustup components only, with the explicit
   `OZAGIA_LANGUAGE_AUTOINSTALL=0` opt-out. System and npm components are
   reported, never auto-installed.**
