# Python Semantic Dialect (v1)

**Status:** Draft spec, paired with `compound_loop_proof.md §Thesis X`. This is the concrete instantiation of S = φ(X_raw) for the Python domain — a native semantic dialect Γ_py whose grammar is designed to sit close to the Shannon lower bound for code-semantic content.

**Scope:** v1 covers pure Python semantics — signatures, types, control flow, data flow, effects. Natural-language intent (docstring prose) is *not* encoded into dialect primitives in v1; it stays as a tagged text field or is dropped entirely depending on task. v2+ may introduce an intent primitive library.

---

## Goals

1. **Entropy-preserving:** φ⁻¹(φ(X)) recovers X up to α-equivalence (renaming, whitespace, comment placement). H(S) ≈ H(X_raw) for reasoning content.
2. **Encoding-efficient:** per-token entropy approaches the tokenizer's maximum for code-semantic content. Each dialect atom is a semantic commitment.
3. **Deterministic compile:** φ is pure, no LLM pass. Cost of running φ must not erode the efficiency gain.
4. **Grammar-closed:** context-free grammar Γ_py, parseable by recursive descent. Grammar-constrained decoding possible.
5. **Tokenizer-friendly:** atoms align with cl100k_base boundaries — prefer single-token ASCII keywords over multi-byte Unicode where equivalent.
6. **Round-trip executable:** φ⁻¹(S) emits valid Python whose `ast.parse()` output is structurally equivalent to the original (semantic equivalence, not byte-for-byte).

## Non-goals (v1)

- Natural-language intent primitives (Wierzbicka-style NSM for code). v2.
- Cross-language — each language gets its own Γ. Python only here.
- Full static-analysis-grade type inference. v1 uses annotations + local inference; unknown types tagged `?`.
- Side-effect discrimination beyond the three-way split (pure / read / mut). v2 expands.

---

## Wire format: Canonical S-expressions

Atoms are whitespace-delimited tokens. Parens denote nesting. The grammar is a subset of Lisp with keyword arguments (`:key value` pairs).

```
expr     ::= atom | list
atom     ::= symbol | string | number | keyword
list     ::= '(' expr* ')'
keyword  ::= ':' symbol
symbol   ::= [a-zA-Z_?@+\-*/<>=!][a-zA-Z0-9_?@+\-*/<>=!]*
string   ::= '"' <utf8-char>* '"'
number   ::= int | float
```

Strings are only used for:
- Docstring text (tagged `:doc "..."`)
- String literals in Python source
- External identifiers (e.g. imported module paths)

All other names are symbols. Canonicalization: module-level names retain source spelling; locally-bound names are α-renamed to `$1`, `$2`, ... in canonical form (optional — only for equivalence-class comparison, not round-trip).

## Top-level structure

A compiled Python module is a single `module` form:

```
(module <module-path>
  :imports (...)
  :decls (...))
```

Where `:imports` is a list of import forms and `:decls` is an ordered list of module-level declarations (functions, classes, assignments, type aliases).

## Declaration forms

### `def` — function

```
(def <name> :: <type-sig>
  :params (<param-spec>...)
  :effects <effect-set>
  :doc <string>?
  :body <body>)
```

- `<name>`: symbol
- `<type-sig>`: `(<param-type>* -> <return-type>)`. Unknown types written `?`.
- `<param-spec>`: `(<name> :default <expr>? :kind pos|kw|varargs|varkw)`
- `<effect-set>`: subset of `{pure, read, mut, io, raise, await}`. Compact: `pure` / `read` / `mut` / `io` / `raise` / `await`; multiple concatenated with `+`, e.g. `read+raise`.
- `:doc` is optional; v1 carries it as opaque string.
- `<body>`: a `block` form (see Control flow).

### `class`

```
(class <name>
  :bases (<name>*)
  :attrs (<attr-spec>*)
  :methods (<def>*))
```

`<attr-spec>`: `(<name> :: <type>)` or `(<name> :: <type> := <expr>)` for defaults.

### `let` — module-level binding

```
(let <name> :: <type> := <expr>)
```

Type is inferred if not annotated.

### `import`

```
(import <module> :as <alias>?)
(from <module> <name>* :as (<alias>...)?)
```

## Type system

Types are written compactly, following mypy/PEP 484 conventions but with abbreviated forms:

| Python type | Dialect |
|---|---|
| `int` | `i` |
| `float` | `f` |
| `bool` | `b` |
| `str` | `s` |
| `bytes` | `B` |
| `None` | `0` |
| `Any` | `?` |
| `list[T]` | `[T]` |
| `tuple[T, U]` | `(T U)` |
| `dict[K, V]` | `{K V}` |
| `set[T]` | `#{T}` |
| `Optional[T]` | `T?` |
| `Union[T, U]` | `T|U` |
| `Callable[[A, B], R]` | `(A B -> R)` |
| User-defined | symbol |

Type variables: `%T`, `%U`, etc. Rationale: single token, disjoint from symbol namespace, legible.

## Effect system

Deterministic classification from AST:

| Operator/call pattern | Effect |
|---|---|
| Pure arithmetic, comparisons, tuple/list construction of pure values | `pure` |
| Attribute/index read on arg | `read` |
| Assignment to attribute/index of arg, `list.append`, `dict[...]=` | `mut` |
| `open`, `print`, `input`, `socket.*`, `requests.*`, `subprocess.*` | `io` |
| `raise <E>` reachable, or call to function with `raise` effect | `raise` |
| `await <expr>` reachable | `await` |

Effects propagate transitively along the call graph. Unresolved calls (unknown function) are `?` — joined via union with known effects.

## Control flow

`block` is an ordered list of statements. Normalizations:

1. **Single-assignment form for locals:** `x = 1; x = x + 2` → `(let $1 1) (let $2 (+ $1 2))`. Enables def-use chain inlining. Optional — toggle via `--ssa`.
2. **Linear block merging:** consecutive unconditional statements become one `(do s1 s2 ...)` form.
3. **Loop canonicalization:** `for x in iter: body` → `(for $x iter body)`. `while cond: body` → `(while cond body)`. `break`/`continue`/`else` preserved as sub-forms.
4. **Exception handlers:** `(try body (except <E> <handler>)* (finally <f>)?)`.
5. **`with` statements:** `(with <ctx-expr> <body>)` — context managers preserved as first-class form.
6. **Decorators:** `(decorated <deco> <def-or-class>)`.

## Expression forms

Operator mapping (keeps Python semantics, compresses surface):

| Python | Dialect |
|---|---|
| `a + b`, `a - b`, `a * b`, `a / b`, `a // b`, `a % b`, `a ** b` | `(+ a b)`, `(- a b)`, `(* a b)`, `(/ a b)`, `(// a b)`, `(% a b)`, `(^ a b)` |
| `a == b`, `a != b`, `a < b`, `a <= b`, `a > b`, `a >= b` | `(= a b)`, `(!= a b)`, `(< a b)`, `(<= a b)`, `(> a b)`, `(>= a b)` |
| `a and b`, `a or b`, `not a` | `(& a b)`, `(\| a b)`, `(! a)` |
| `a in b`, `a is b` | `(in a b)`, `(is a b)` |
| `a.b` | `(. a b)` |
| `a[b]` | `([] a b)` |
| `a[b:c:d]` | `(slice a b c d)` |
| `f(a, b=c, *d, **e)` | `(call f a :b c :* d :** e)` |
| `lambda a, b: e` | `(\ a b e)` |
| `[x for x in xs if p]` | `(for/list $x xs :if p)` |
| `{k: v for k in ks}` | `(for/dict $k ks v)` |
| String literals | `"..."` |
| f-strings | `(f "..." <expr>...)` with `{i}` slots |
| `None`, `True`, `False` | `0`, `t`, `n` |

## Data flow (optional annotation)

For modules where data flow matters (e.g. program analysis), declarations carry explicit `:uses` and `:defines` lists:

```
(def transform :: ([i] -> [i])
  :params (($xs))
  :effects pure
  :uses ($xs len)
  :defines ($out)
  :body (block
    (let $out (for/list $x $xs (* 2 $x)))
    $out))
```

`:uses` and `:defines` are reconstructible from the body; emitting them explicitly trades ~3% length for O(1) lookup during static analysis. Default: omit.

## α-equivalence and round-trip

Two dialect expressions are α-equivalent if they agree after renaming bound variables canonically (`$1`, `$2`, ...) in order of binding site.

**φ⁻¹ (Python emitter):**
- Module-level names preserved from source
- `$N` names become `v0`, `v1`, ... (or original name if source is retained in a debug-mode sidecar)
- Types: unannotated in emitted Python unless annotation existed in source (toggled by `--preserve-types`)
- Effects: dropped on emission (Python has no effect syntax)
- Normalized control flow expanded to idiomatic Python

The round-trip guarantee is **structural equivalence at the AST level** (same `ast.dump()` modulo identifier renaming and annotation preservation), not byte-for-byte source preservation.

## Empirical targets (for Theorem X validation)

1. **Token length ratio on coding tasks:** measure L(X_raw) / L(S) distribution across HumanEval, MBPP, LiveCodeBench problem sets. Target: 5×–200× range as claimed in §Thesis X empirical instance.
2. **Grammar-inexpressibility ε:** fraction of AST nodes in a corpus that Γ_py cannot express. Target: ε < 0.5% on CPython stdlib.
3. **Round-trip fidelity:** φ⁻¹(φ(X)) executes with same test results as X on the ozmalib test suite. Target: 100% on v1 covered surface.
4. **Quality preservation under matched budget:** pass@1 on HumanEval 164 under Γ_py arm ≥ pass@1 under X_raw arm.
5. **Cost ratio bound:** Cost(S) / Cost(X_raw) ≤ L(S) / L(X_raw) confirmed by measured wall-clock under identical decoding settings.

## Open issues for v2

- **Intent primitives.** Docstrings currently carry as opaque text. An NSM-style primitive library (`any_pair`, `fold`, `filter`, `map`, `search`, `sort`, `transform`) would tighten the encoding further on spec-heavy tasks but requires a corpus.
- **Cross-module flow.** v1 compiles per-module; cross-module data flow loses resolution. v2 needs a project-level φ.
- **Dynamic attribute access / `getattr` / metaprogramming.** Currently tagged `?` for type and `io` for effect. v2 needs a richer effect/provenance model for metaprogramming-heavy code.
- **C extensions.** Opaque — tagged `?`/`io`. Out of scope.
- **Decorator semantics.** Decorated functions carry the decorator as a wrapper form but do not resolve its effect transformation. Accurate only for introspectable decorators.

## Relationship to existing artifacts

- **IR (`research/ozmalib/protocol/ir_maps/python.yaml`):** substitution cipher — `def → ƒ`, `return → →`, `import → ←`. Wire-level compression (~5.5× on code). Orthogonal to the dialect. Can be applied on top of Γ_py for additional surface-level savings; main work is done by Γ_py.
- **Compress pipeline (`research/ozmalib/compress/pipeline.py`):** Tree-sitter stubbing that keeps signatures, drops bodies. Task-specific lossy reduction for retrieval; not an entropy-preserving translation. Complementary to Γ_py — use stubs for context retrieval, Γ_py for the focused reasoning window.
- **Map/graph memory (`research/ozmalib/graph_memory/`):** persistent semantic map. Γ_py is the serialization format for code elements entering the map; nodes and edges in the map mirror dialect forms.
