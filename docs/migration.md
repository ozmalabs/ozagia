# Python→Rust migration overlay (Phase 6)

Status: implemented 2026-11-23. This is the initial setup of a graph-aware
migration cockpit, **not** a Python-to-Rust transpiler. It answers:

- which Python public names have a Rust counterpart;
- which are planned, in progress, ported, diverged, dropped or Rust-only;
- which facts differ (arity, async, visibility, docs, signature shape);
- what a person or agent should do next, and where to look.

Nothing here compiles Rust, imports or executes the target project, reaches the
network, calls `cargo metadata` or runs a model. The one write path is an
explicit `ozagia migration scaffold --write`, which appends a suggested
skeleton through the normal edit transaction.

## Configuration

```toml
[migration]
python = "python"      # repository-relative directory, must exist
rust = "crates"        # repository-relative directory, must exist
infer = true           # report candidates; never accept them

[[migration.pairs]]
python = "pkg.models.User"
rust = "core::model::User"
status = "in_progress" # optional manual override
```

Validation is strict and happens when the project configuration loads:

| Rule | Result |
|---|---|
| `python`/`rust` outside the repository (including through a symlink) | `ConfigError` |
| `python`/`rust` does not exist or is not a directory | `ConfigError` |
| either tree missing when `[migration]` is present | `ConfigError` |
| unknown key in `[migration]` or `[[migration.pairs]]` | `ConfigError` |
| pair without `python` or `rust` | `ConfigError` |
| duplicate `(python, rust)`, duplicate Python key or duplicate Rust key | `ConfigError` |
| unknown `status` | `ConfigError` |
| more than 1000 pairs | `ConfigError` |

Pair names are bounded dotted or `::` symbol paths. They are not required to
exist in either inventory: a planned pair may name a Rust symbol that does not
exist yet. If neither side resolves, the pair is reported as unresolved rather
than guessed.

The section is supported in `.ozagia.toml`. The versioned
`ozagia/config.toml` may set `migration.python`, `migration.rust` and
`migration.infer` (registry keys `migration.*`); `[[migration.pairs]]` has no
registry equivalent and is reported as an unmapped key with that reason. The
effective configuration is exposed by `ozagia doctor` (`config.migration`) and
by `ozagia config get migration.python` (and the other two registry keys).

## Commands and MCP tool

```bash
ozagia migration status                 # concise text; writes a report
ozagia migration status --json          # full JSON report
ozagia migration status --markdown      # bounded Markdown report
ozagia migration status --no-record     # do not write the report

ozagia migration tasks                  # Markdown checklist
ozagia migration tasks --json --limit 20

ozagia migration scaffold pkg.models.User --target crates/core/src/model.rs
ozagia migration scaffold pkg.models.User --target crates/core/src/model.rs --write
```

`status` scans both configured trees and writes `status.json` and `status.md`
under the state directory (`<root>/.ozagia/migration/` by default, or
`$OZAGIA_STATE_DIR/migration/`). `--no-record` keeps it read-only.
`tasks` never writes a report or creates a goal. `scaffold` is a preview unless
`--write` is given; `--base-sha HASH` refuses a stale write.

The MCP tool `dev_migration` takes `view = status | tasks | scaffold` and a
`limit` (default 25, max 100). Its output is bounded and it is **always
read-only**; a scaffold is previewed, never written. Use the CLI for the
explicit write transaction.

## Inventories

Both inventories are bounded, sorted and deterministic. Defaults: 4000 files,
512 KiB per file, 32 MiB in total and 20 000 symbols per tree; every skipped
file is reported as an omission.

### Python

The Python side reuses `ozagia.core.memory._python_parse` (the stdlib AST parser
the graph uses) and adds `__all__`, dataclass fields and `*args`/`**kwargs`
markers.

- A top-level name is API when it is public (`name` does not start with `_`) and,
  if the module defines a literal `__all__`, is listed there. A public name
  omitted from `__all__`, and a public method of an API class, stay in the
  inventory as non-API symbols.
- A dynamic `__all__`, an undefined `__all__` name or an unparseable file is an
  explicit omission.
- Fields and signatures come from annotations. `self`/`cls` are excluded; for a
  dataclass the field names are the compared arguments.
- `async def` is recorded; decorators and the first doc line are kept.

### Rust

The Rust side reuses `ozagia.index.rust_symbols.parse_rust` (tree-sitter with a
regex fallback) and reads the nearest `Cargo.toml` with stdlib `tomllib` for the
crate name (metadata only; no Cargo command runs).

- A top-level item is API when its visibility starts with `pub` and it is not a
  module/impl. `pub(crate)` is public inside the crate and is flagged when it
  differs from a Python public API name.
- Trait items and their methods are recorded; trait methods are public by trait
  semantics. Inherent `impl` methods are public only when marked `pub`.
- Keys use crate paths: `fixturecrate::models::User`, with methods as
  `fixturecrate::models::User::display_name`.
- Struct fields are read from the parsed source span (named and tuple structs).
- `#[test]` functions are recorded but excluded from migration status when they
  are not API.

Known inventory gaps: re-exports are not resolved; `#[path]`, `include!`,
`cfg`-generated modules and macros that generate items are not expanded;
integration-test modules get a path-derived module name; custom Cargo target
layouts are not deep-linked. These are reported as limits, never guessed.

## Status vocabulary

| Status | Meaning | How it is assigned |
|---|---|---|
| `planned` | Python symbol, no Rust counterpart (yet) | automatic for an unmatched Python API symbol, or a manual override |
| `in_progress` | work is acknowledged and not finished | manual only |
| `ported` | both sides exist and no parity flag is set | derived for an explicit pair, or manual |
| `diverged` | both sides exist and at least one parity flag is set | derived for an explicit pair, or manual |
| `dropped` | intentionally not ported | manual only |
| `extra` | Rust symbol with no Python source | automatic for an unmatched Rust API symbol, or manual |

Explicit pairs win over inference and derived status; a manual status wins over
derived status and keeps the parity flags as facts. Inferred candidates never
change a status. A pair with neither side resolved is reported under
`unresolved_pairs`.

### Parity flags

Flags are deterministic facts about the two inventories:

| Flag | Meaning |
|---|---|
| `missing_counterpart` | only one side resolved |
| `kind` | one side is a type-shaped item, the other is callable |
| `arity` | parameter count (callables) or field count (types) differs |
| `async` | one side is async and the other is not |
| `visibility` | Python public API versus Rust `pub(crate)`/restricted visibility |
| `doc` | one side has a doc first line and the other does not |
| `signature` | parameter/field names differ, or a return annotation exists on only one side |

The overlay does **not** compare types across languages (`str` versus `String`
is not a signature mismatch by itself), imports, lifetimes, ownership, error
behaviour or test results. A clean flag set means "nothing in this inventory
disagrees", not "the port is correct".

## Construct and type table

The table is also in `src/ozagia/migration/hints.py` and is deterministic. Each
hint has `support = supported | manual | unsupported`, a reason, and
`compilable: false` in JSON. No hint is a claim that the output compiles.

| Python construct | Rust suggestion | Support | Reason |
|---|---|---|---|
| `class` | struct or enum | manual | data versus behaviour and inheritance are design decisions |
| `@dataclass` | struct + `#[derive(...)]` | supported | fields become public fields; only Debug/Clone are scaffolded |
| `Protocol` | trait | manual | generic bound, `dyn Trait` or enum; object safety and receivers |
| `Enum` | enum | supported | simple variants map; payloads and methods need review |
| exception class | `Result<T, E>` + error enum | manual | raise sites and callers must change |
| `async def` | `async fn` | supported | runtime/executor choice is manual |
| `Optional[T]` | `Option<T>` | manual | every `None` path is a decision |
| `Union[A, B]` / `A \| B` | enum (or trait object) | manual | named variants and matches |
| `list[T]` | `Vec<T>` | supported | ownership/borrowing still need review |
| `dict[K, V]` | `HashMap<K, V>` | supported | keys need `Eq + Hash`; order is unspecified |
| `set[T]` | `HashSet<T>` | supported | elements need `Eq + Hash` |
| `tuple[A, B]` | `(A, B)` | supported | variadic tuples become `Vec<T>` |
| `str` | `String` or `&str` | supported (choice) | owned versus borrowed is a design choice |
| `Callable[..., T]` | `Fn`/`FnMut`/`FnOnce` or trait object | manual | captures, lifetimes, dispatch |
| `Any` | explicit dynamic boundary | manual | no implicit Rust `Any` |
| int | `i64` | supported (choice) | integer width and overflow policy |
| float | `f64` | supported | direct |
| bool | `bool` | supported | direct |
| bytes | `Vec<u8>` | supported | direct |
| `Decimal`, `datetime`, `date` | crate type | manual | crate and policy choices |

### Unsupported or manual-only

- **Metaprogramming** (metaclasses, `__getattr__`, dynamic class creation):
  unsupported. Rust macros cannot preserve arbitrary runtime behaviour.
- **Monkeypatching** (runtime attribute replacement in tests or production):
  unsupported. Rust resolves methods statically.
- **C extensions / `ctypes` / `cffi`**: unsupported. FFI ownership, linkage and
  the build are outside this static overlay.
- **`**kwargs`**: unsupported. Prefer a typed struct; there is no direct
  equivalent.
- **Keyword-only parameters**: manual. Rust has no direct equivalent; change
  the call shape deliberately.
- **`*args`**: manual. Choose a slice, iterator or overload set.
- **Decorators**: manual. Port the behaviour, not the syntax. A property,
  `staticmethod`, cache or framework decorator needs a deliberate shape.
- Properties, abstract methods, default argument values, `__post_init__`,
  exception control flow and operator overloads are not translated.

## Scaffold preview

`ozagia migration scaffold SYMBOL --target FILE` accepts only:

- a public top-level dataclass with no bases, no methods, at least one annotated
  field and simple scalar fields (`int`, `float`, `bool`, `str`, `bytes`, `None`);
- a public top-level function (sync or async) with no decorators, no
  `*args`/`**kwargs`, every parameter annotated and a return annotation, using
  only the same simple scalar types.

It refuses with a reason when the shape is a protocol, enum, exception, plain
class, union, optional, decorated function, unknown/user type, unannotated
parameter or return, a dataclass with methods or bases, or a symbol that is not
found/ ambiguous. It also refuses a target outside the Rust tree, a non-`.rs`
target, a target that does not already exist, and a duplicate symbol name.

The skeleton is explicitly marked as a suggestion:

- dataclass → `#[derive(Debug, Clone)] pub struct ...` with public fields;
- function → `pub [async] fn ... { todo!("port from ...") }`;
- no imports, error type, lifetimes, serde derives or behavioural translation.

Preview reads the target through `edit/txn.read_source` and computes the new
sha256; it writes nothing. `--write` calls `edit/txn.write_source`: optional
base-hash check, atomic replace, backup under `.ozagia/edits/backups/`, and a
tree-sitter parse gate when `tree-sitter-rust` is installed. New files are not
created implicitly; add the file (or a `mod` declaration) yourself, then
scaffold into it.

## Reports, task hand-off and freshness

`status` returns:

- effective config, per-tree file/symbol/API counts and backends;
- per-status totals, all rows (bounded), top gaps and top divergences;
- inferred candidates with confidence and `accepted: false`;
- unresolved pairs and explicit omissions (size/file/symbol limits, parse
  failures, dynamic `__all__`, entries ignored by the inventory);
- freshness relative to the last report;
- scan limits and trust notes.

`tasks` emits one checklist item per planned, in-progress or diverged symbol.
Success criteria use the existing evidence vocabulary:

| Criterion | Evidence | Meaning |
|---|---|---|
| `counterpart_exists` | `observed` | an inventory row with a stable path exists |
| `parity_flags_clear` | `verified` | arity/async/visibility/doc/signature facts agree |
| `mapped_tests_pass` | `verified` | the mapped tests pass (never inferred from a green outer status) |
| `divergence_explained` | `observed` | every current flag is resolved or recorded as intentional |

It creates no goal and runs no test or task. The payload states
`created_goals: false` and `executed: false`.

Freshness compares content fingerprints of both scanned trees and a digest of
the effective migration configuration with the previous report. The first run is
`unknown`; unchanged inputs are `fresh`; any change is `stale` with the changed
side named. A stale report never silently keeps old status.

## Trust limits

- No graph `migrates_to` edge is written yet; the overlay is a report over fresh
  inventories, not a snapshot mutation.
- Inference is a name/module heuristic. Confidence is a hint, not a probability
  of semantic equivalence.
- Public API comparison is syntactic. Re-exports, dynamic attributes and
  runtime-registered types are invisible.
- Tests are not mapped between languages by this phase. `mapped_tests_pass`
  means the project's own mapping exists and passes; the report does not invent
  one.
- Scaffolds are intentionally narrow. A refusal is the honest outcome for
  anything that needs design work.
- Reports live only in the state directory. `status`, `tasks` and scaffold
  previews never modify either source tree.
