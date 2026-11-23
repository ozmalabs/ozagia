# Multi-language tags — Workstream D

**Status:** v0, landed 2026-11-14. Dev tooling, not agent runtime.

`src/ozagia/index/tag_languages.py` is a suffix-keyed registry of tree-sitter tag
backends. It extends the repo map's Python/Rust/Lua tag extraction to
TypeScript, TSX, JavaScript, Go, Java, C, C++, Kotlin and Swift without
changing the tag-cache or reference-graph code. Each backend uses the grammar
package's own `queries/tags.scm` when it ships one; Kotlin uses a small query
bundled in this package, and Python keeps the stdlib-AST fast path already used
by `ocelot_repo_map`.

Tags are **syntactic hints** for the repo map. They are not scope-resolved,
they are not compiler-accurate, and they never edit code.

## Registry shape

`LANGUAGES` maps a file suffix to one spec dict:

```python
LANGUAGES[".rs"] == {
    "language": "rust",
    "module": "tree_sitter_rust",
    "tags_query": "queries/tags.scm",
    "factory": "language",
    "backend": "tree-sitter",
}
```

| Suffixes | `language` | Grammar module | Language factory | Backend | Query defs | Query refs |
|---|---|---|---|---|---|---|
| `.py` | `python` | `tree_sitter_python` | `language` | `python-ast` | yes (AST) | yes (AST) |
| `.rs` | `rust` | `tree_sitter_rust` | `language` | tree-sitter | yes | yes (bundled query) |
| `.lua` | `lua` | `tree_sitter_lua` | `language` | tree-sitter | yes | yes |
| `.ts` | `typescript` | `tree_sitter_typescript` | `language_typescript` | tree-sitter | yes | yes |
| `.tsx` | `tsx` | `tree_sitter_typescript` | `language_tsx` | tree-sitter | yes | yes |
| `.js`, `.jsx` | `javascript` | `tree_sitter_typescript` | `language_tsx` | tree-sitter | yes (TS forms only) | yes |
| `.go` | `go` | `tree_sitter_go` | `language` | tree-sitter | yes | yes |
| `.java` | `java` | `tree_sitter_java` | `language` | tree-sitter | yes | yes |
| `.c`, `.h` | `c` | `tree_sitter_c` | `language` | tree-sitter | yes | **no** |
| `.cpp`, `.cc`, `.cxx`, `.hpp` | `cpp` | `tree_sitter_cpp` | `language` | tree-sitter | yes | **no** |
| `.kt`, `.kts` | `kotlin` | `tree_sitter_kotlin` | `language` | tree-sitter | yes | yes |
| `.swift` | `swift` | `tree_sitter_swift` | `language` | tree-sitter | yes | **no** |

`tags_query` is package-relative unless `query_package` names a package (Kotlin
and Rust use bundled queries under `src/ozagia/index/queries/`). `backend_status()`
resolves it to the installed absolute path and reports whether the query
actually contains `@definition.*` / `@reference.*` captures. `.js`/`.jsx` use
the TSX factory because that grammar is the JavaScript + JSX superset; plain
JavaScript parses there too.

Kotlin is the one entry with `query_package: "ozagia"`: the
`tree-sitter-kotlin` PyPI wheel (1.1.0) ships no `queries/tags.scm`, so the
registry points at `src/ozagia/index/queries/kotlin-tags.scm`. The loader
resolves that path through the named package instead of the grammar. Swift
uses its package query (`tree_sitter_swift/queries/tags.scm`, 0.7.4).

## API

```python
from ozagia.index.tag_languages import (
    LANGUAGES,
    language_for,
    extract_tags,
    available_languages,
    backend_status,
)

entry = language_for("src/App.TSX")     # registry dict, or None
entry["language"]                       # "tsx"
entry["module"]                         # "tree_sitter_typescript"

tags = extract_tags("src/App.tsx", source)
# [{"name": "AppProps", "kind": "def", "detail": "definition.interface",
#   "line": 12, "end_line": 24, "qualname": "AppProps"}, ...]

available_languages()
# ["c", "cpp", "go", "java", "javascript", "kotlin", "lua", "python",
#  "rust", "swift", "tsx", "typescript"]  (sorted, installed-only)

backend_status("c")
# {
#   "language": "c", "available": True, "backend": "tree-sitter",
#   "module": "tree_sitter_c", "factory": "language",
#   "tags_query": "/…/tree_sitter_c/queries/tags.scm",
#   "definitions": True, "references": False,
#   "suffixes": [".c", ".h"], "error": None,
# }
```

`extract_tags` returns definitions, references and Python imports:

```python
{"name": "AppProps", "kind": "def", "detail": "definition.interface",
 "line": 12, "end_line": 24, "qualname": "AppProps"}
{"name": "useThing", "kind": "ref", "detail": "reference.call",
 "line": 21, "scope": "App"}
{"name": "helper", "kind": "import", "detail": "import.from",
 "line": 3, "module": "pkg.helpers", "level": 0,
 "imported": "helper", "scope": ""}
```

`detail` is the query capture (`definition.function`, `reference.call`, …),
`line` is 1-based, and `end_line`/`qualname`/`scope` are the enclosing-def
information attached after extraction. Python's definitions and references are
deliberately identical to `ocelot_repo_map.extract_tags` for `.py` files:
class/function definitions, loaded names (`reference.name`) and loaded
attributes (`reference.attribute`); its import bindings are the extra
`kind: "import"` records.

`language_for()` is suffix-only and case-insensitive; the file need not exist.
`backend_status()` accepts a language label (`"rust"`) or a suffix (`".rs"`),
and never raises.

## Query mechanics

For tree-sitter backends:

1. Import the grammar module lazily and call its language factory.
2. Wrap it in `Language(...)` and compile the package's `queries/tags.scm`.
3. Parse UTF-8 source and iterate the query cursor.
4. tree-sitter 0.26 yields `(pattern_index, {capture: [Node, ...]})` from
   `QueryCursor.matches()`. For each match, the first capture starting with
   `definition.` or `reference.` selects the kind; every node captured as
   `name` becomes a tag.

Language objects and compiled queries are cached per
`(module, factory, query package, query)`; parsers are created per call so
callers do not share parser state. A grammar import, query compile, or per-file
parse failure returns `[]` for that file. `backend_status()` carries the reason,
so a caller can distinguish "no tags in this file" from "backend unavailable".

Python does not touch tree-sitter: `ast.parse` + `ast.walk` supplies the same
tags as the repo map. Its AST path remains available even when
`tree_sitter_python` is not installed.

## Which shipped `tags.scm` queries worked

Nine of the ten installed grammar packages expose `queries/tags.scm`; the
Kotlin wheel does not, so ozagia bundles one. The granularity differs:

- **Python** (`tree_sitter_python`) — query exists, but the AST path is used.
  Grammar query covers classes, functions, module-level constants and calls.
- **Rust** — ozagia bundles `index/queries/rust-tags.scm` as a superset of the
  upstream query: structs/enums/unions/type aliases, methods, functions,
  traits, trait method signatures, modules, macros, constants, statics and enum
  variants; call/macro/impl references plus qualified calls such as
  `Foo::bar()`. An `impl` scope capture makes methods `Foo.bar` instead of a
  bare `bar`. `use` declarations are emitted by the Rust source model as
  `kind: "import"` records with `detail: "import.use"` (module, imported name,
  alias, glob and scope); they are not CST name references. Resolution of those
  imports is Phase 2 of the Rust plan.
- **Lua** — functions, methods, assignment/table function definitions; call
  references.
- **TypeScript / TSX** — interfaces, abstract classes, function/method
  signatures and modules; type-annotation and `new` references. The upstream
  query does **not** tag ordinary `function_declaration`, `class_declaration`,
  `method_definition` or variable declarations, and has no call references.
  This is an upstream `tree-sitter-typescript` 0.23.2 gap, not a registry bug.
- **JavaScript** — same module and query as TypeScript/TSX. Because the
  upstream query has no ordinary JS function/class patterns, JavaScript
  definitions are only found for TS declaration forms (for example
  `interface`/`module`); `new` references work.
- **Go** — functions, methods, named types; call and `type_identifier`
  references. The query's package/import/var/const patterns have no
  `definition.*`/`reference.*` capture, so they are ignored. Because every
  `type_identifier` is a reference, a definition name can also appear as a
  reference; duplicates are preserved as occurrences.
- **Java** — classes, methods, interfaces; call, implements-list, object
  creation and superclass references.
- **C** — structs/unions, functions, typedefs, enums. **No reference captures.**
- **C++** — structs/unions, functions (including field/qualified declarators),
  typedefs, enums, classes. **No reference captures.**
- **Kotlin** — ozagia's bundled query covers `class_declaration` (classes,
  interfaces, enum classes), `object_declaration`, named `companion_object`,
  `enum_entry`, `function_declaration`, `property_declaration` and `type_alias`
  definitions; references are simple calls, navigation calls and `user_type`
  identifiers. The grammar wheel ships no tags query and its `queries/`
  directory is absent, so there is no upstream query to defer to.
- **Swift** — the shipped query covers class/struct/enum/extension
  declarations (`definition.class` for all of them), protocols
  (`definition.interface`), methods, `init`/`deinit` and properties. **No
  reference captures.** It marks the enclosing `class_declaration` as the
  definition node for method/property patterns, so a class member can be
  captured twice: once with the whole class range and once with the member
  range. Names and lines are correct; the duplicate occurrence is preserved,
  and `qualname` for the wider capture can be misleading. The repo map's
  symbol list de-duplicates by `(name, line)`.

The C/C++ and Swift reference gaps are reported directly by
`backend_status()` (`references: False`). All gaps are explicit skips with
reasons in the test suite rather than hidden.

## Integration notes for `ocelot_repo_map.py`

The shared repo map is intentionally not edited here. The central integrator
can merge the registry with a small delegation:

```python
from ozagia.index.tag_languages import LANGUAGES as TAG_LANGUAGES
from ozagia.index.tag_languages import backend_status, language_for
from ozagia.index.tag_languages import extract_tags as tag_language_tags

# Suffix membership for _source_files(); values are module names for
# compatibility with the existing _grammar()/diagnostics code.
LANGUAGES = {
    suffix: entry["module"] for suffix, entry in TAG_LANGUAGES.items()
}

def extract_tags(path, source):
    return tag_language_tags(path, source)
```

Then:

1. Keep `_python_tags`/the `.py` branch only if you want the repo map's own
   copy; otherwise delete it and let the registry dispatch `.py` to the
   identical AST path.
2. `_grammar()` and `_tag_query_path()` become unused for extraction. If they
   are kept for diagnostics, route them through `language_for()` and
   `backend_status()` rather than duplicating module/factory knowledge.
3. `_source_files()` now sees all 18 suffixes through the `LANGUAGES` mapping,
   provided the relevant files live under its `SOURCE_DIRS`. Extend
   `SOURCE_DIRS` separately if TypeScript/Go/Java/C/C++/Kotlin/Swift trees live
   elsewhere.
4. Rust extraction semantics changed in Phase 1 of the Rust plan (impl
   scopes, trait signatures, constants, qualified calls and `use` import
   records), so `TAGS_SCHEMA_VERSION` and `repo_map.CACHE_VERSION` are both 4.
   Extractor fingerprints invalidate the old Rust rows automatically; bump the
   version again only when existing-language extraction semantics change.
5. `backend_status()` is suitable for a one-line brief/diagnostic (for example
   "tags: python(AST), rust, lua, typescript/tsx/js, go, java, c/cpp
   (definition-only), kotlin, swift (definition-only)") and to explain empty
   tags for a file.
6. The registry's `extract_tags` swallows per-file failures exactly like the
   repo map. Do not use `available_languages()` as a per-file gate; call
   `extract_tags` directly and keep the empty-list fallback.

## Tests

```bash
PYTHONPATH=$PWD/src .venv/bin/python -m pytest -q tests/test_tag_languages.py
# 62 passed, 4 skipped (with both new grammars installed)
```

Skips are explicit and upstream-driven:

```text
SKIPPED  javascript: upstream tags.scm declares no definition capture for this
         snippet's declaration form
SKIPPED  c: upstream tags.scm has no reference captures
SKIPPED  cpp: upstream tags.scm has no reference captures
SKIPPED  swift: upstream tags.scm has no reference captures
```

A genuinely missing grammar skips the affected language instead of failing the
registry; with neither Kotlin nor Swift installed, their extraction cases and
the repo-map integration case skip cleanly.

Covered: the exact suffix table and factory choices; case-insensitive
`language_for`; sorted/unique/usable `available_languages`; honest unknown
status; definition and reference extraction for every installed grammar, with
explicit skips for the upstream capture gaps; capture-class reporting; output
shape and determinism; exact Python parity with
`ocelot_repo_map.extract_tags`; syntax-error and unknown-suffix empties; JSX in
`.js`/`.jsx`/`.tsx`; C-family suffix aliases; Kotlin bundled-query resolution,
realistic class/function nesting and `.kts` scripts; Swift
struct/protocol/function captures; and graceful degradation when a grammar
import fails. `tests/test_repo_map.py` additionally checks that `.kt` and
`.swift` files are listed by `_source_files()` and tagged through the cache
when the grammar is available.

## Honest limits

- **Syntactic, not semantic.** Same-named symbols across files can create false
  edges; aliases, re-exports, dynamic dispatch and overload resolution are
  invisible. LSP/SCIP remains the precise layer.
- **Upstream query gaps.** TypeScript/JavaScript ordinary declarations and all
  call expressions are missing from the shipped TypeScript query; C, C++ and
  Swift ship definition-only queries. Swift also classifies structs, enums and
  extensions as `definition.class` and repeats class-member captures through
  its class-scoped patterns. Extending or replacing those queries is a
  follow-up, not a registry change.
- **Kotlin bundled query.** It is deliberately small: it does not capture
  constructors, lambda parameters, annotations, package/import names or
  type-parameter declarations, and a qualified call contributes every
  identifier in its receiver chain (so `users.map` yields references to
  `users` and `map`). Because no upstream query exists, future grammar releases
  may rename nodes; the query is compiled at load time, so a mismatch surfaces
  as a reported backend error rather than silent wrong tags.
- **File-level graph.** Tags feed the existing file reference graph; there is
  no symbol-level call graph.
- **Suffix-based language choice.** `.h` is treated as C, `.hpp`/`.cc`/`.cxx`
  as C++, and `.js`/`.jsx` as TSX grammar. A C++ project with `.h` headers will
  parse them with the C grammar; content sniffing or project config is not
  implemented.
- **Duplicate occurrences.** A capture per occurrence is preserved, so a name
  can appear multiple times on one line or as both a definition and a
  reference (notably Go `type_identifier`). The graph treats them as weights,
  not unique symbols.
- **No external libraries.** Tags only describe the parsed file; imports are
  not resolved to definitions outside the indexed set.
- **Availability is install-time.** `available_languages()` reflects
  importable grammar modules and compilable queries in the current
  environment; it does not benchmark parse quality.

## Files

- `src/ozagia/index/tag_languages.py` — registry, lazy backend loader, tag
  extraction, availability/status diagnostics.
- `src/ozagia/index/queries/kotlin-tags.scm` — bundled Kotlin tags query (the
  grammar wheel ships none).
- `tests/test_tag_languages.py` — per-grammar and registry tests.
- `docs/tag_languages.md` — this document.

## Source

- `src/ozagia/core/repo_map.py` — existing tag cache, reference graph and Python
  AST fast path (not modified by this workstream).
- `docs/features/ADVANCED_CODING_AGENT_PLAN.md` — Workstream D.
- `docs/features/GRAPH_CODING_LANDSCAPE.md` — multi-language tags gap (P2).
