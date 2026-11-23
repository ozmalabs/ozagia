# Rust support

Status: Phase 0 baseline and Phase 0.5 language support packages of
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
| Tags and repo map | tree-sitter definitions and references from `tree_sitter_rust/queries/tags.scm`: structs, enums, unions, type aliases, functions, methods, traits, modules, macros, calls, macro calls, impl references. `use` imports are **not** tagged yet. | install the `treesitter` extra (`tree-sitter-rust`); `tag_languages.backend_status("rust")` |
| Graph symbols | line-based `_rust_parse` regex: `fn`, `struct`, `enum`, `trait`, `impl`, `mod`, `type`, `const`, `static`; one line per symbol; no docs, args, returns or nesting | `build_index` / `ozagia-memory build` |
| Module edges | `mod x;` resolves to `x.rs` or `x/mod.rs`, with crate-root rules (see below); `#[path = "..."]` is recorded as unsupported, not guessed | graph `File -imports-> File` edges |
| Symbol questions | `dev_symbol_*` / `dev_callers` / `dev_callees` work over the tags, but resolution is name-based for Rust: `use` imports are not resolved, and there is no type inference | `ozagia core.symbol_graph` CLI or the MCP tools |
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

- `#[path = "..."]` on a module declaration is recorded in the file's
  `rust_unsupported_mods` attribute and produces no edge.
- Inline `mod x { ... }` blocks are not tracked as scopes; `mod y;` inside one
  is attributed to the file, not the inline module.
- Custom Cargo target paths (`[lib] path`, `[[bin]] path`) are not read yet, so
  a non-conventional crate root is treated as an ordinary module file.
- A helper module named `tests/*.rs` (rather than `tests/*/mod.rs`) is treated
  as a test crate root; its own children resolve beside it.

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

- The graph's Rust parser is a line regex, not the tree-sitter tag extractor:
  spans are one line, doc comments and signatures are empty, methods do not
  carry their `impl` type, and `use` bindings are invisible.
- Repo-map edges are name-based. Generic names are capped, and two unrelated
  `new` functions can be linked by name; the LSP layer is the precise one.
- `cargo test` is not a captured command and there are no crate/test-target
  nodes, so no affected-test selection or test-result cache exists for Rust.
- No build, `cargo check` or `cargo metadata` is run: reading the project must
  not execute build scripts or proc macros. Cargo tests, when supported, will
  run only because the caller typed the command.
- `#[path]`, inline module scope, custom target paths and generated source are
  reported as gaps; they are not followable guesses.

## Checks

```bash
# focused
PYTHONPATH=src .venv/bin/python -m pytest -q \
  tests/test_rust_mods.py tests/test_repo_map.py tests/test_memory.py \
  tests/test_graph_reingest.py

# LSP is gated and needs the rustup component
OZAGIA_LIVE_LSP=1 PYTHONPATH=src .venv/bin/python -m pytest -q tests/test_lsp.py
```
