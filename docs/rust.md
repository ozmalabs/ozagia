# Rust support

Status: Phases 0, 0.5, 1 and 2 of
[`docs/plans/2026-11-23_rust_parity.md`](plans/2026-11-23_rust_parity.md),
2026-11-23. This page describes what works today, not the target state. The
`ozagia languages` command and automatic component checks are documented in
[`languages.md`](languages.md).

Rust is a first-class indexed language at the syntactic layer and a
second-class one at the semantic layer. The plan closes the rest; this page is
updated as each phase lands.

## Matrix

| Capability | Today | How to enable / verify |
|---|---|---|
| Tags and repo map | bundled `index/queries/rust-tags.scm` superset: structs/enums/unions/type aliases, functions, impl-scoped methods (`Foo.bar`), traits, trait signatures, modules, macros, constants, statics, enum variants, calls, qualified calls and impl references; `use` bindings are import records | install the `treesitter` extra (`tree-sitter-rust`); `tag_languages.backend_status("rust")` |
| Graph symbols | tree-sitter structural parser: qualified names, spans, doc comments, attributes, arguments, return type, visibility and test markers; regex fallback without the grammar | `build_index` / `ozagia-memory build` |
| Test nodes | inline `#[test]`/`#[tokio::test]`/`#[rstest]` functions, plus `tests/*.rs` and `benches/*.rs` files, produce Test nodes; `cargo test` capture and selection remain Phase 5 | graph Test nodes, `tests_for` |
| Module edges | `mod x;` resolves to `x.rs` or `x/mod.rs`, with crate-root rules (see below); `#[path = "..."]` is recorded as unsupported, not guessed | graph `File -imports-> File` edges |
| Symbol questions | `dev_symbol_*` / `dev_callers` / `dev_callees` resolve Rust `crate`/`self`/`super`/workspace imports, aliases, globs and bounded re-exports; external crates are unresolved, never guessed by name; there is no type inference | `ozagia core.symbol_graph` CLI or the MCP tools |
| Structural edits | CST subtree ops on `.rs` through `edit/cst.py`: `replace_node`, `insert_before_node`, `insert_after_node`, `delete_node`, `wrap_node`, `replace_span`; preview by default | `ozagia edit` script with a `file` ending in `.rs` and a CST path |
| Lint | `rustfmt --check --edition <Cargo edition>` when `rustfmt` is on `PATH`; config `[lint] rust = "auto" "rustfmt" "off"` | `ozagia-settings`; edit-time lint |
| LSP | rust-analyzer through `ozagia.lsp`: definition, references, document symbols, hover, diagnostics, call hierarchy, rename preview | `ozagia languages ensure --root .` or `ozagia[lang-rust]`; `OZAGIA_LIVE_LSP=1` for the gated smoke |
| Dependencies and review | Cargo.lock inventory, crates.io detection, `pkg:cargo/…` purls, strict SemVer advisory matching, `cargo-audit` in the allowlisted scanners | `ozagia-review`, deep review dependencies subject |
| SCIP | the importer is language-agnostic; a Rust index can be produced with `rust-analyzer scip .` and `scip print --json` (recipe is provisional until Phase 7) | `index/scip.py` |
| `cargo test` selection | not captured; there are no Rust test-target nodes yet | Phase 5 |
| Migration assistance | not implemented | Phase 6 |

## Module resolution (Phase 0)

`mod x;` is resolved from the declaring file, not just its directory:

| Declaring file | `mod x;` looks for |
|---|---|
| `src/lib.rs`, `src/main.rs`, `build.rs` | `src/x.rs`, `src/x/mod.rs` |
| `src/bin/tool.rs`, `examples/e.rs`, `tests/t.rs` (crate roots) | beside the root file |
| `src/foo.rs` | `src/foo/x.rs`, `src/foo/x/mod.rs` |
| `src/foo/mod.rs` | `src/foo/x.rs`, `src/foo/x/mod.rs` |
| `src/foo/bar.rs` | `src/foo/bar/x.rs`, `src/foo/bar/x/mod.rs` |

A module declaration that appears in both layouts (`foo.rs` and `foo/mod.rs`)
uses `foo.rs`, matching rustc's preference order; the graph records both
candidates so reingest notices a file moving between layouts.

Known gaps, deliberately reported rather than guessed:

- `#[path = "..."]` resolves the simple relative form; an attribute that
  depends on a generated or non-standard module layout is reported as
  unresolved rather than guessed.
- Inline `mod x { ... }` blocks scope symbols as `x.fn` and external
  `mod y;` declarations inside them use the inline module directory.
- Custom Cargo target paths are read from `[lib]`/`[[bin]]`/`[[test]]`/
  `[[bench]]`; targets produced by build scripts, conditional `cfg`, or macros
  are not discovered.
- A helper module named `tests/*.rs` (rather than `tests/*/mod.rs`) is treated
  as a test crate root; its own children resolve beside it.

## Structural symbols and tags (Phase 1)

`src/ozagia/index/rust_symbols.py` parses each `.rs` file with tree-sitter-rust
when it is installed and falls back to the line parser otherwise. Symbols carry
the Python-parity field set: `name`, `qualname`, `kind`, `start_line`,
`end_line`, `doc`, `decorators` (attributes), `args`, `returns`, `visibility`
and `is_test`. Methods inside an `impl`/trait are qualified by the implementing
type (`Foo.bar`, `Trait.qux`); inline modules nest as `module.name`; duplicate
qualnames in one file get the same `#2`/`#3` suffix as Python. Outlines and the
repo map use the qualified names.

`use` declarations are expanded (single paths, groups, `self`, aliases, globs,
`pub use`, `crate`/`super`/`self` prefixes) into `kind: "import"` tag records.
Test attributes are recorded, inline/source tests become Test nodes, and
`tests/*.rs`, `benches/*.rs` files are indexed with the test role.

## Import and module resolution (Phase 2)

`src/ozagia/core/rust_modules.py` reads `Cargo.toml` files (stdlib `tomllib`,
never `cargo metadata`) and builds crate targets and a module tree:

- library targets (`[lib]` name/path or `src/lib.rs`), binaries (`src/main.rs`,
  `src/bin/*.rs`, `[[bin]]`) and test/bench targets (`[[test]]`/`[[bench]]`,
  `tests/*.rs`, `benches/*.rs`, `*/main.rs`);
- external `mod foo;` files (`foo.rs` or `foo/mod.rs`), nested module
  directories, inline modules as a file plus qualname prefix, and `#[path]`
  declarations;
- workspace member crates discovered from their own `Cargo.toml` files.

`use` records resolve through `crate::`, `self::`, `super::`, leading `::`,
workspace crate names, aliases and groups. A glob resolves to its module file;
a matching symbol can be linked when exactly one definition has the imported
qualname. `pub use` re-exports are followed for up to three hops. External
crates (`serde`, `std`, …) are reported as external and never fall back to a
same-named local symbol. Ambiguous module paths and missing files are reported
as unresolved.

The resolved imports produce:

- symbol-graph edges with `resolution: "import"` and confidence 1.0 for exact
  targets; unresolved/external bindings suppress the bare-name fallback for
  that reference;
- file-level `File -> File` repo-map edges (including glob module targets), so
  Rust imports contribute to PageRank;
- per-graph counters (`rust_module_targets`, `resolved_import`,
  `unresolved`) in `build_symbol_graph`.

## LSP

The registry entry is `rust-analyzer`; the client starts it over stdio and
uses the file's workspace root. Rust has a support package
(`ozagia[lang-rust]` plus the `rust-analyzer` and `rustfmt` rustup
components); `ozagia languages ensure --root .` installs what is missing and
reports it. The manual equivalent is:

```bash
uv pip install --python .venv/bin/python 'ozagia[lang-rust]'
# fallback: python -m pip install 'ozagia[lang-rust]'
rustup component add rust-analyzer rustfmt
rust-analyzer --version
```

The dev environment has the component (1.97.1 at the time of writing), and
`ozagia index`, `ozagia agent` and `ozagia-memory build` run the language check
automatically. Set `OZAGIA_LANGUAGE_AUTOINSTALL=0` to disable that, or pass
`--no-language-install` to the memory build.
Rust-analyzer indexes a Cargo workspace lazily: the first definition or
reference query can return an empty answer, then a `content modified` protocol
error, before the project is ready. The client reports the empty answer
honestly; the gated live smoke can still skip on a cold server. Phase 4 adds
the bounded warm-up/retry and the full live suite.

## Edits

`.rs` currently has CST-node addressing, not symbol addressing:

```json
{"file": "src/lib.rs",
 "base_sha256": "<optional>",
 "ops": [{"op": "replace_node", "target": "child[1].child[3]", "text": "..."}]}
```

Preview is the default; applying backs up the file under
`.ozagia/edits/backups/` and writes atomically. There is no symbol rename,
`use` insertion or symbol-body replacement yet; Phase 3 adds those in
`edit/rust_edit.py`. Raw diff/patch input is refused.

## Honest limits

- Imports and module paths are resolved syntactically, not with type
  inference. Paths through a module alias (`use crate::a as m; m::f()`),
  conditional `cfg` inclusion, feature-specific modules and macros/proc-macros
  are not fully resolved; generics are stripped from impl scope names.
- Re-exports are followed up to three hops. Glob imports resolve to a module
  file and only link a symbol when the imported qualname is unique.
- Repo-map edges are name-based. Generic names are capped, and two unrelated
  `new` functions can be linked by name; the LSP layer is the precise one.
- `cargo test` is not a captured command. Test nodes exist, but affected-test
  selection, crate/target mapping and the result cache remain Phase 5.
- No build, `cargo check` or `cargo metadata` is run: reading the project must
  not execute build scripts or proc macros. Cargo tests, when supported, will
  run only because the caller typed the command.
- `#[path]`, inline module scope, custom target paths and generated source are
  reported as gaps; they are not followable guesses.

## Checks

```bash
# focused
PYTHONPATH=src .venv/bin/python -m pytest -q \
  tests/test_rust_modules.py tests/test_rust_symbols.py \
  tests/test_rust_mods.py tests/test_symbol_graph.py \
  tests/test_tag_languages.py tests/test_repo_map.py \
  tests/test_memory.py tests/test_graph_reingest.py

# LSP is gated and needs the rustup component
OZAGIA_LIVE_LSP=1 PYTHONPATH=src .venv/bin/python -m pytest -q tests/test_lsp.py
```
