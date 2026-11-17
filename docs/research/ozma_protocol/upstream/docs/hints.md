# Hints Engine

*The key to winning is cheating.*

Hints are YAML files that detect patterns in user messages and inject guidance
into model context before the model sees the request. They rig the deck so the
model gets expert knowledge exactly when it needs it — zero tokens wasted when
it doesn't.

Hints replace prompts, skills, .cursorrules, copilot-instructions, and agent
system prompts. They do everything these formats do, but conditionally — firing
only when relevant instead of burning tokens on every request. See
[hints-vs-prompts.md](hints-vs-prompts.md) for the full argument.

259 hints ship with ozma. Adding a new one takes about 2 minutes.

## How it works

```
User message
    │
    ▼
┌──────────────┐
│  load_hints  │  ← loads + compiles YAML once at startup
└──────────────┘
    │
    ▼
┌──────────────┐
│  check_hints │  ← regex match: detect ∧ ¬guard → fire
└──────────────┘
    │
    ▼
┌──────────────┐
│ format_hints │  ← build system message block
└──────────────┘
    │
    ▼
  Injected as system message before the model prompt
```

1. **Detect**: regex patterns matched against the user's message. ANY match fires
   the hint (unless `match_all: true`, which requires ALL patterns to match).
2. **Guard**: regex patterns that suppress the hint. If ANY guard matches, the
   hint is skipped. Guards prevent false positives (e.g., "explain SQL injection"
   should not trigger the SQL injection fix hint).
3. **Guidance**: the text injected into context. Written for the model, not the user.

Cost: ~0.02ms per hint check (compiled regex only, no model calls).

## API

```python
from ozmalib.hints import load_hints, check_hints, format_hints

# Load all built-in hints (once, at startup)
hints = load_hints()

# Load with project root — auto-converts .cursorrules/CLAUDE.md/etc.
hints = load_hints(project_root="/path/to/my-project")

# Also load project-specific hints from extra directories
hints = load_hints("/path/to/my-hints", "/another/dir")

# Check a message against loaded hints
matched = check_hints(
    "create an alembic migration for the users table",
    hints,
    types={"tool"},       # optional: only check these types
    max_hints=5,          # optional: cap number of results
    max_tokens=1000,      # optional: cap total guidance tokens
)

# Format for system message injection
block = format_hints(matched)
# → "## Tool Usage Guidance (auto-detected)\n\nAlembic migrations should be..."

# Porting hints have a richer formatter
from ozmalib.hints import format_porting_hint
for h in matched:
    if h.type == "porting":
        block = format_porting_hint(h)
```

### `load_hints(*extra_dirs, project_root, auto_convert_guidance) -> HintSet`

Loads YAML files from the built-in `data/` directory plus any extra directories.
Later directories override earlier ones by `name` (so project hints can replace
built-in ones). Returns compiled rules ready for matching.

When `project_root` is provided, also loads `{project_root}/.hints/` and
auto-converts any guidance files (.cursorrules, CLAUDE.md, etc.) via a cheap
LLM call. Set `auto_convert_guidance=False` to disable auto-conversion.

### `check_hints(message, hints, *, types, normalize, file_context, language, max_hints, max_tokens) -> list[Hint]`

| Parameter | Type | Default | Description |
|---|---|---|---|
| `message` | `str` | required | The user's message to match against |
| `hints` | `HintSet` | `None` (loads built-in) | Pre-loaded hint set |
| `types` | `set[str]` | `None` (all) | Only check hints of these types |
| `normalize` | `Callable` | `None` | Optional text normalizer (typo correction) |
| `file_context` | `str` | `None` | Code content for `detect_code` patterns |
| `language` | `str` | `None` (auto-detect) | Override language for param substitution |
| `max_hints` | `int` | `0` (unlimited) | Maximum hints to return |
| `max_tokens` | `int` | `0` (unlimited) | Approximate token budget for guidance text |

Returns `list[Hint]` sorted by `priority * confidence` descending.

### `format_hints(hints, *, header, include_detail, include_examples) -> str`

| Parameter | Type | Default | Description |
|---|---|---|---|
| `hints` | `list[Hint]` | required | Matched hints to format |
| `header` | `str` | `None` (auto) | Header line |
| `include_detail` | `bool` | `False` | Include rich detail field when available |
| `include_examples` | `bool` | `False` | Include before/after code examples |

When compression frees token budget, enable `include_detail` and
`include_examples` to inject richer guidance at the same effective cost.

### `Hint` dataclass

| Field | Type | Description |
|---|---|---|
| `name` | `str` | Hint identifier (from YAML `name` field) |
| `type` | `str` | Category: `tool`, `security`, `porting`, etc. |
| `guidance` | `str` | The guidance text to inject |
| `confidence` | `float` | 0–1, based on how many patterns matched |
| `meta` | `dict` | All extra YAML fields (severity, source, phases, etc.) |
| `detail` | `str` | Rich guidance, included when budget allows |
| `examples` | `list[HintExample]` | Before/after code pairs |

## Hint file spec

A hint file is a YAML document with detection patterns and guidance text.
Place it in `ozma/hints/data/` (built-in) or any directory passed to
`load_hints()`.

### Required fields

| Field | Type | Description |
|---|---|---|
| `name` | `str` | Unique identifier. Used for override/dedup. Falls back to filename stem if omitted. |
| `detect` | `list[str]` | Regex patterns (case-insensitive). ANY match fires the hint. |
| `guidance` | `str` | Text injected into model context. Write for the model, not the user. |

### Optional fields

| Field | Type | Default | Description |
|---|---|---|---|
| `type` | `str` | `"custom"` | Category for filtering. Common: `tool`, `security`, `porting`, `benchmark`, `framework`, `mobile`, `language`, `testing`, `reliability`, `correctness`, `performance`. |
| `guard` | `list[str]` | `[]` | Regex patterns. ANY match suppresses the hint. Use to prevent false positives. |
| `detect_code` | `list[str]` | `[]` | Regex patterns matched only against `file_context` (code content), not the message. |
| `match_all` | `bool` | `false` | If `true`, ALL `detect` + `detect_code` patterns must match (not just any one). |
| `priority` | `int` | `50` | 1–100. Higher = more important. Used in sorting: `priority * confidence`. |
| `detail` | `str` | `""` | Rich guidance included when token budget allows. Use for deeper explanations, edge cases, and rationale. |
| `examples` | `list[dict]` | `[]` | Before/after code pairs. Each dict has `before`, `after`, and optional `label`. Code examples compress well via Ozma IR. |
| `params` | `dict[str, dict]` | `{}` | Language-specific variable substitution. Keys are language names, values are `{variable: value}` dicts. `{variable}` placeholders in guidance/detail/examples get replaced. |

### Metadata fields (pass-through)

Any field not in the list above is stored in `Hint.meta` and passed through to
consumers. Common metadata fields by type:

**Tool hints:**
- `command`: The CLI command to run instead of hand-writing

**Security hints:**
- `severity`: `critical`, `high`, `medium`, `low`

**Porting hints:**
- `source`, `target`: Source and target framework/language
- `strategy`: Migration strategy (`strangler fig`, `incremental`, `parallel auth`)
- `phases`: Ordered list of migration phases
- `traps`: Known pitfalls
- `equivalents`: Map of source concept → target equivalent

**Benchmark hints:**
- `source`: Paper/benchmark citation
- `severity`: For security-related benchmark hints

### Confidence scoring

Confidence is calculated automatically from the number of pattern matches:

- Base: 0.50 (one pattern matched)
- +0.15 per additional pattern match
- Capped at 1.0

Results are sorted by `priority * confidence` (descending), then trimmed to
`max_hints` / `max_tokens` budget.

### Naming conventions

```
{type}-{topic}.yaml          # general: security-sql-injection.yaml
{type}-{subtopic}.yaml       # specific: tool-alembic.yaml
bench-{category}-{detail}.yaml  # benchmark-mined: bench-wrong-mapping-graph.yaml
porting-{source}-{target}.yaml  # migrations: porting-django-fastapi.yaml
```

## Examples

### Minimal hint (3 fields)

```yaml
name: dont-hand-write-migrations
type: tool
detect:
  - '\b(?:create|write)\b.*\bmigration\b'
guidance: |
  Don't hand-write database migrations. Run the autogenerate command instead.
```

### Tool hint with guard

```yaml
name: alembic
type: tool
command: "alembic revision --autogenerate -m '<description>'"

detect:
  - '\b(?:create|write|add|make|generate)\b.*\b(?:alembic|migration)\b'
  - '\b(?:database|db|schema)\s+(?:migration|change|update)\b'

guard:
  - '\b(?:fix|edit|modify|debug|review)\b.*\b(?:migration|alembic)\b'
  - '\bdowngrade\b'
  - '\b(?:explain|how does|what is)\b.*\balembic\b'

guidance: |
  Alembic migrations should be generated from model changes, not hand-written.
  Make the SQLAlchemy model changes first, then run autogenerate:
    alembic revision --autogenerate -m '<description>'
```

### Security hint with detail + examples + params

```yaml
name: sql-injection
type: security
severity: critical
priority: 75

detect:
  - '\b(?:sql|query|select|insert|update|delete)\b.*\b(?:format|f-string|concatenat)\b'
  - '\braw\s*(?:sql|query)\b'
  - '\bexecute\s*\('

guard:
  - '\b(?:parameterized|prepared|placeholder)\b'
  - '\b(?:explain|what is)\b.*\bsql injection\b'

guidance: |
  SQL INJECTION RISK: Never construct SQL by string formatting.
  Use {placeholder} parameterized queries or {orm}.

detail: |
  51.4% of security patch failures use the wrong repair strategy.
  The fix is parameterization, not escaping/sanitization.
  Escaping is fragile — parameterized queries make injection
  structurally impossible.

examples:
  - label: "Direct query"
    before: |
      cursor.execute(f"SELECT * FROM users WHERE name = '{name}'")
    after: |
      cursor.execute("SELECT * FROM users WHERE name = {placeholder}", (name,))

params:
  python:
    placeholder: "?"
    orm: "SQLAlchemy select()"
  javascript:
    placeholder: "$1"
    orm: "Prisma findMany()"
  go:
    placeholder: "$1"
    orm: "sqlx or GORM"
```

When matched against "fix this raw sql query in python", the `{placeholder}`
and `{orm}` variables are filled from the `python` params. The `detail` and
`examples` are included when `format_hints(include_detail=True, include_examples=True)`
is called — typically when compression has freed token budget.

### Porting hint with phases and traps

```yaml
name: django-to-fastapi
type: porting
source: Django
target: FastAPI
strategy: strangler fig

detect:
  - '\b(?:port|migrate|convert|rewrite)\b.*\bdjango\b.*\bfastapi\b'
  - '\bdjango\s+to\s+fastapi\b'

guard:
  - '\b(?:compare|vs|difference|choose)\b.*\bdjango\b.*\bfastapi\b'

phases:
  - "1. Models: Port SQLAlchemy models from Django ORM."
  - "2. Auth: Replace Django auth with FastAPI dependencies."
  - "3. Views: Django views -> FastAPI route handlers."

traps:
  - "Django lazy loading: SQLAlchemy requires explicit eager loading."
  - "Signals: Replace with explicit event dispatch."

equivalents:
  models.Model: "SQLModel"
  ViewSet: "APIRouter with CRUD routes"
  settings.py: "pydantic-settings BaseSettings"

guidance: |
  MIGRATION: Django -> FastAPI
  Recommended strategy: strangler fig
```

### Benchmark-mined hint with priority

```yaml
name: wrong-mapping-graph
type: benchmark
source: "arXiv 2511.04355"
priority: 60

detect:
  - '\b(?:shortest|longest)\s+path\b'
  - '\b(?:connected\s+components?|strongly\s+connected)\b'
  - '\bcycle\s+detect(?:ion)?\b'

guard:
  - '\b(?:explain|what is|how does)\b'
  - '\b(?:BFS|DFS|dijkstra|bellman|floyd)\b'

guidance: |
  PROBLEM CLASSIFICATION: This looks like a graph problem.
  Before coding, verify the structure:
    - Pairwise relationships -> graph (BFS/DFS/Dijkstra)
    - Shortest path, non-negative weights -> Dijkstra
    - Ordering constraints -> topological sort
  Common mistake: applying DP or greedy to graph traversal problems.
```

### match_all: require multiple signals

```yaml
name: mobile-manifest
type: mobile
match_all: true

detect:
  - '\b(?:mobile|ios|android|react\s*native|expo)\b'
  - '\b(?:sync|update|keep.*in.*sync|mirror)\b'

guard:
  - '\b(?:compare|vs|explain|what\s+is)\b'

guidance: |
  Check for .mobile-sync.yaml in the repo root before making changes.
```

## Overriding built-in hints

To override a built-in hint, create a YAML file with the same `name` in a
directory passed as an extra dir to `load_hints()`. Later directories win:

```python
# Your project-specific alembic hint replaces the built-in one
hints = load_hints("/my-project/hints")
```

To suppress a built-in hint entirely, override it with an empty detect list
(it will be skipped during loading).

## Writing good hints

1. **Write guidance for the model, not the user.** The model reads this, not a
   human. Be direct and specific: "Use X, not Y" beats "Consider using X".

2. **Detect what the user is trying to do, not what they said.** Multiple
   phrasings of the same intent ("create migration", "add migration", "new
   migration") should all match.

3. **Guard aggressively.** False positives waste tokens and annoy users. Guard
   against explanation requests, comparisons, and fixing/debugging (vs creating).

4. **Keep guidance short, put depth in detail.** The `guidance` field (3–6 lines)
   always fires. Put deeper explanations, edge cases, and rationale in `detail` —
   it's included only when token budget allows.

5. **Show, don't just tell.** Use `examples` for before/after code pairs. The
   most powerful hint isn't "use parameterized queries" — it's showing the wrong
   code and the right code. Examples compress well via Ozma IR.

6. **Use params for multi-language hints.** Instead of separate YAML files per
   language, use `params` with `{variable}` placeholders. One hint covers all
   languages, auto-parameterized by detected language.

7. **One concern per hint.** Don't combine SQL injection and XSS into one hint.
   Separate hints can fire independently and be prioritized separately.

6. **Test with `check_hints()`.** Run your hint against example messages to
   verify detection and guard behavior before shipping.

## Current hint inventory

| Type | Count | Examples |
|---|---|---|
| framework | 54 | django-orm-fq, django-orm-prefetch, django-custom-managers, sklearn-estimator-api, sklearn-validation, flask-context, nextjs-server-components, react-hooks-rules, vue-composition-api, angular-signals, svelte-runes, express-middleware, rails-n-plus-one, spring-transaction, pandas-indexing, pandas-groupby, numpy-broadcasting, astropy-units, requests-session, sqlalchemy-session, pydantic-models |
| security | 23 | sql-injection, xss, auth, csrf, idor, file-upload, rate-limiting, clickjacking, jwt, cors, password-hashing, hardcoded-secrets, deserialization |
| correctness | 14 | python-iterator-exhaustion, python-comparison-protocol, python-descriptor-protocol, python-copy-semantics, python-context-manager-protocol, python-super-mro, python-exception-chaining, python-property-inheritance |
| porting | 13 | django-fastapi, python-rust, rails-explicit, passport-nextauth |
| language | 17 | rust-ownership, go-error-handling, go-goroutine-leaks, typescript-strict-mode, python-mutable-defaults, python-async-patterns, logging-config, pathlib-patterns, regex-python, collections-patterns, itertools-patterns |
| algorithm | 12 | algo-greedy-correctness, algo-binary-search-answer, algo-monotonic-stack, algo-two-pointer, algo-sliding-window, algo-dp-state, algo-modular-arithmetic, algo-union-find, algo-prefix-sum, algo-heap-priority |
| meta | 10 | meta-read-test-first, meta-minimal-diff, meta-regression-check, meta-match-imports, meta-match-style, meta-error-interpretation, meta-check-return-type, meta-api-boundary, meta-writing-hints, meta-convert-to-hints |
| benchmark | 19 | wrong-mapping-graph, edge-cases-empty, format-precision, format-return-type, security-race-condition, security-integer-overflow, migration-api-deprecation |
| testing | 8 | mock-boundaries, snapshot, flaky, test-isolation, pytest-fixtures, pytest-parametrize, pytest-monkeypatch |
| tool | 10 | alembic, prisma, prettier, terraform, openapi, vite-config |
| reliability | 8 | connection-pool, graceful-shutdown, database-indexing, api-versioning, websocket-patterns, redis-patterns |
| mobile | 8 | manifest, components, nativewind, navigation, ios, android |
| infrastructure | 4 | github-actions, terraform-state, docker, k8s-resources |
| other | 18 | data, performance, async, config, workflow, sphinx-extensions, matplotlib-axes, sympy-assumptions |
| **Total** | **218** | |

## Auto-conversion on startup

When `load_hints(project_root="/path/to/project")` is called, the engine
automatically:

1. **Detects** guidance files in the project root:
   `.cursorrules`, `.cursor/rules/*.mdc`, `CLAUDE.md`,
   `.github/copilot-instructions.md`, `.clinerules/*.md`,
   `.windsurfrules`, `.windsurf/rules/*.md`, `.github/AGENTS.md`

2. **Converts** each file to hint YAML via a cheap LLM call (Haiku or
   GPT-4o-mini — whichever API key is available)

3. **Caches** results in `.hints/.converted/` with content hashing — files
   are only re-converted when they change

4. **Loads** converted hints alongside built-in and project hints

No user action required. If no API key is set, conversion is silently skipped.
ozma's own guidance files (containing "ozma is available in this
project") are automatically excluded.

```python
# Auto-convert + load everything
hints = load_hints(project_root="/path/to/my-project")

# Skip auto-conversion (just load built-in + .hints/)
hints = load_hints(project_root="/path/to/my-project", auto_convert_guidance=False)

# Manual conversion of a single file
from ozmalib.hints import convert_guidance_file, detect_guidance_files

files = detect_guidance_files(Path("/path/to/my-project"))
for path, source_type in files:
    hint_dicts = convert_guidance_file(path, source_type)
```

### Cache structure

```
.hints/
├── .converted/           # auto-converted hints (gitignored)
│   ├── manifest.json     # content hashes for cache invalidation
│   ├── converted-use-hooks.yaml
│   └── converted-strict-mode.yaml
├── my-project-hint.yaml  # hand-written project hints
└── ...
```

## Migrating from prompts/skills/cursorrules

Hints replace all other guidance formats. Three paths:

### Automatic (recommended)

Just pass `project_root` to `load_hints()`. Your `.cursorrules`, `CLAUDE.md`,
and other guidance files are auto-converted and loaded as hints. Zero effort.

### Assisted

Ask the model to "convert my .cursorrules to hints" or "convert my CLAUDE.md
to hints" — the `convert-to-hints` hint fires automatically and guides the
model through the decomposition process.

### Manual

Decompose the source file into individual guidance atoms, write
detect/guard patterns for each, and drop YAML files in `.hints/`.

### What remains

The minimal prompt (~50–100 tokens):
```
You are working on [project]. [One sentence description.]
Packages: [list]. [Output format if needed.]
```

Everything else becomes hints — either auto-converted or in `.hints/`.

See [hints-vs-prompts.md](hints-vs-prompts.md) for the full comparison.
