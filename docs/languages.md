# Language support packages

Status: initial implementation (2026-11-23), covering the ten languages in the
tag registry. Each language has a package (a `lang-*` extra plus a support
module) that declares the components it needs. When ozagia encounters a
language in a checkout, it reports the missing components and — for the ones it
can install safely — reports that it is installing them for that language.

This is deliberately separate from [`ozagia-toolchain`](toolchain.md), which
remains discovery-only and never installs anything. Language support is the
explicit installer for parser and language-server components.

## Command

```bash
ozagia languages list [--json]
ozagia languages detect [--root R] [--json]
ozagia languages ensure [--root R] [--json] [--no-install] [--no-network]
```

- `detect` is read-only: it reports each detected language, its components and
  readiness.
- `ensure` reports missing components and installs the installable ones. It
  never uses sudo, never runs an OS package manager and never executes project
  code. `--no-install` reports the commands instead.
- `ozagia-memory build` also checks languages before building. `ozagia index`,
  `ozagia agent` and the first `ozagia replace` build do the same.
- Disable the automatic path with `OZAGIA_LANGUAGE_AUTOINSTALL=0` or, for the
  memory build, `--no-language-install`.

Example message when Rust support is incomplete:

```text
[ozagia] languages: detected Rust support (.rs)
[ozagia] languages: installing required components for Rust: Rust grammar via uv pip ozagia[lang-rust]; Rust language server via rustup
[ozagia] languages: could not install rust-analyzer: exit 1: ...
[ozagia] languages: Rust still missing: rust-analyzer
```

A ready checkout is silent: nothing is printed when all required components are
present. System and global-npm components (clangd, Kotlin/Swift servers,
TypeScript's server) are reported with their manual command, never installed
implicitly.

## Packages

| Language | Suffixes | Extra | Automatic components | Manual / optional |
|---|---|---|---|---|
| Python | `.py`, `.pyi` | `ozagia[lang-python]` | `tree-sitter-python`, `pyright`, `ruff` | — |
| Rust | `.rs` | `ozagia[lang-rust]` | `tree-sitter-rust`, `rust-analyzer` (rustup), `rustfmt` (rustup) | — |
| Lua | `.lua` | `ozagia[lang-lua]` | `tree-sitter-lua` | Lua language server not wired |
| TypeScript / JavaScript | `.ts`, `.tsx`, `.js`, `.jsx` | `ozagia[lang-typescript]` | `tree-sitter-typescript` | `typescript-language-server` via npm |
| Go | `.go` | `ozagia[lang-go]` | `tree-sitter-go` | Go language server not wired |
| Java | `.java` | `ozagia[lang-java]` | `tree-sitter-java` | Java language server not wired |
| C | `.c`, `.h` | `ozagia[lang-c]` | `tree-sitter-c` | `clangd` via the system package manager |
| C++ | `.cpp`, `.cc`, `.cxx`, `.hpp`, `.hh` | `ozagia[lang-cpp]` | `tree-sitter-cpp` | `clangd` via the system package manager |
| Kotlin | `.kt`, `.kts` | `ozagia[lang-kotlin]` | `tree-sitter-kotlin` | `kotlin-language-server` |
| Swift | `.swift` | `ozagia[lang-swift]` | `tree-sitter-swift` | `sourcekit-lsp` (Swift toolchain) |

Installing a language extra by hand is equivalent for the Python components.
uv is preferred; pip remains the fallback:

```bash
uv pip install --python .venv/bin/python 'ozagia[lang-rust]'
# fallback
python -m pip install 'ozagia[lang-rust]'
```

## What "components" means

Each `src/ozagia/languages/<language>.py` exports a `LanguageSupport` with:

- `suffixes`: the source suffixes that select it;
- `components`: everything the support package wants, each with a probe
  (`importlib.util.find_spec` for Python modules, `shutil.which` plus
  `<root>/.venv/bin` for binaries), a manager (`pip`, `rustup`, `npm`,
  `system`, `none`), a package/spec, an optional `ozagia[...]` extra, and
  `required` / `requires_network` / `requires_sudo` flags.

Readiness means every **required** component is installed. Optional components
(linters, some language servers) are reported but do not make a language
"missing".

## Safety and limits

- Only two install strategies run automatically: `uv pip install` (falling
  back to `python -m pip install`) of the package spec declared in the support
  module, and `rustup component add` of a component declared there. No shell,
  no sudo, no npm, no OS package manager.
- Installation is bounded (600 s per command) and failures are reported; a
  failed install never fails the build or session. The command still runs with
  the available fallback (for Rust, the regex symbol parser and tags).
- Detection scans the checkout for suffixes, skipping the same generic
  directories as the graph (`target`, `node_modules`, `.venv`, `.ozagia`, …)
  and bounded to 50,000 files. A truncated scan is reported, not hidden.
- `pyproject.toml` keeps the union `treesitter` extra for existing installs;
  the `lang-*` extras are additive.
- Adding a language means adding its module, listing it in
  `src/ozagia/languages/__init__.py`, and adding its `lang-*` extra. The
  registry test checks that every suffix in the tag registry has a package.

## Files and tests

- `src/ozagia/languages/model.py` — components, probes, status, bounded
  installs, detection.
- `src/ozagia/languages/*.py` — one package per language.
- `src/ozagia/languages_cli.py` — `ozagia languages`.
- `tests/test_languages.py` — registry coverage, detection, install reporting,
  manual/network paths, CLI.

```bash
PYTHONPATH=src .venv/bin/python -m pytest -q tests/test_languages.py
ozagia languages detect
```
