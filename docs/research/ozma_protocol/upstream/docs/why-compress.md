# Why compress? Every model has 1M context now.

All frontier models have 1M token context windows. Claude Sonnet 4.6, Opus 4.6,
GPT-5.4 — they can all swallow your entire codebase in one gulp. So why bother
compressing?

**Because fitting isn't the point. Cost and quality are.**

We ran the same cross-module bug report through three models with two different
context strategies: compressed stubs (function signatures only) vs raw code
(every line of every file). The results speak for themselves.

---

## The experiment: find a real bug in Flask

**Bug**: Flask teardown callbacks silently stop executing when one raises an
exception. `do_teardown_request` and `do_teardown_appcontext` iterate callbacks
in a bare loop — first exception kills the chain, remaining cleanup never runs.
Database connections leak, file handles stay open, monitoring hooks never fire.

**Fix**: [pallets/flask@fbb6f0b](https://github.com/pallets/flask/commit/fbb6f0b)
(Feb 2026) — added `_CollectErrors` context manager, wraps each callback
individually. 3 files changed across `app.py`, `ctx.py`, and `helpers.py`.

**Task**: given the full Flask codebase (24 files, ~9,400 lines) + a bug
report, identify the root cause, trace the cross-module interaction, and
propose a fix.

### Results

| Model | Context | Input tokens | Output tokens | Cost | Time | Found bug? |
|---|---|---:|---:|---:|---:|---|
| Gemini 2.5 Flash | **Stubs** (23K) | 26,328 | 1,852 | **$0.005** | 11.5s | Yes |
| Claude Haiku 4.5 | **Stubs** (28K) | 27,660 | 1,370 | **$0.028** | 11.0s | Yes |
| Claude Sonnet 4.6 | Raw (94K) | 93,516 | 1,351 | **$0.301** | 27.7s | Yes |

**Sonnet with raw code costs 60x more than Flash with stubs. 11x more than
Haiku with stubs. All three found the exact same bug.**

Every model identified the same root cause (`do_teardown_request` and
`do_teardown_appcontext` in `app.py`), traced the same call chain through
`ctx.py`, and proposed the same collect-and-reraise fix. Quality is identical.
The raw code is pure waste.

### Why this works

Compressed stubs keep every function signature and docstring. The model sees the
entire call graph at a glance:

```
ctx.pop() → app.do_teardown_request() → loop(func(exc)) ← NO try/except
```

With raw code, the model wades through 76K tokens of implementation detail —
template rendering, URL routing, session handling — none of which is relevant to
the teardown bug. It finds the answer anyway, but it takes 3x longer and costs
10-60x more.

---

## The cost multiplier is bigger than you think

The naive calculation: 94K raw / 23K stubs = 4x more input tokens. But that
understates the real multiplier because the model doesn't just **read** more —
it **writes** more.

With 302K tokens of raw code (tested on Django REST Framework), the model
generates ~2.7x more output tokens:

- Longer reasoning chains to search through implementation detail
- More analysis explaining what it found in all that code
- More intermediate steps when used as an agent
- Re-reading and re-processing sections it's already seen

Output tokens cost 3-5x more than input. So the output overhead hits your
bill **harder** than the input bloat. The effective multiplier is 5-10x, not
3-4x.

---

## The cost of failure

The real killer isn't a successful refactor that costs more. It's a **failed**
one — where the model misses call sites buried in implementation detail, and
you burn tokens on 3 round-trips fixing inconsistencies.

Tested on Django REST Framework: rename `is_valid()` → `validate()` across
23 files, 277 occurrences, 302K tokens of raw code.

| Model | Compressed (1 shot) | Raw (3 retries) | Wasted |
|---|---:|---:|---|
| Claude Sonnet 4.6 | $0.32 | $3.09 | **9.6x** |
| Claude Opus 4.6 | $0.54 | $5.16 | **9.6x** |
| GPT-5.4 | $0.26 | $4.92 | **18.9x** |

GPT-5.4 gets hit worst: 2x input pricing above 272K tokens, plus the output
overhead on every retry. Three failed attempts cost nearly **19x** what a single
compressed attempt costs.

### At scale: 100 refactors/month

| Strategy | Monthly cost (Sonnet 4.6) |
|---|---:|
| Compressed, 1 shot | $32 |
| Raw, 1 shot | $103 |
| Raw + 30% failure rate | $165 |

The API savings are a rounding error next to developer time. Each failed
refactor costs 45-60 minutes of context-switching, manual verification, and
inconsistency debugging. 100 refactors x 50 minutes saved = **83 hours/month**.

---

## What compression keeps vs removes

| Kept | Removed |
|---|---|
| Function signatures | Function bodies |
| Class definitions | Implementation detail |
| Docstrings | Comments |
| Import statements | Blank lines |
| Type annotations | Decorators (configurable) |

The model needs to know **what exists and how it connects**. It does not need to
read every line of every function body to rename a method or trace a call chain.

---

## Reproduce it yourself

```bash
pip install ozmalib

# Generate demo prompts
python scripts/demo_bug_finding.py    # Flask bug
python scripts/demo_refactor.py       # DRF refactor

# Run through models (needs OPENROUTER_API_KEY in .env)
python scripts/run_demo.py bug                         # Haiku stubs vs Sonnet raw
python scripts/run_demo.py bug --stub-model flash      # Flash stubs vs Sonnet raw
python scripts/run_demo.py refactor                    # DRF rename
```

The prompts are written to `/tmp/demo_bug_stub_prompt.txt` and
`/tmp/demo_bug_raw_prompt.txt`. Feed them to any model and compare.

---

## Framework ports: where compression changes everything

Refactoring is renaming things. Porting is rewriting them. And when you're
porting, you literally **do not care about the function bodies** — you're
throwing them away and rewriting them in the target framework. The stubs ARE
the spec.

### The Django → FastAPI problem

Django is the worst case. Its contracts are vibe-based — implicit, spread
across files, and baked into framework magic:

- Model `Meta` classes silently configure database behavior
- Class-based views dispatch through 6 layers of `super()` calls
- Middleware ordering in `settings.py` determines what runs when
- Signal handlers fire from registration in `apps.py`, not from call sites
- The ORM's lazy evaluation means a QuerySet in a serializer does something
  completely different than the same QuerySet in a view
- `related_name` conventions wire up reverse lookups you'll never find by
  searching for them
- Template context processors inject variables with no explicit import

None of this is in function signatures. All of it breaks when you port to
FastAPI's explicit dependency injection model.

### What happens with raw code

Feed a model 300K tokens of raw Django code and ask it to port to FastAPI.
The model reads every line of Django implementation detail and **reproduces
Django patterns in FastAPI**:

- Wraps FastAPI routes in class-based views (Django habit)
- Implements middleware as ASGI wrappers that mimic Django's
  `process_request`/`process_response` chain (FastAPI uses dependency injection)
- Creates an ORM-like lazy evaluation layer on top of SQLAlchemy (FastAPI
  uses explicit async queries)
- Builds a signals system because the Django code used signals (FastAPI
  doesn't need them — the explicit architecture makes the coupling visible)

Each of these is wrong. Each requires a round-trip to fix. Each round-trip
burns 300K input tokens plus the model's bloated output. After 3-4 rounds of
"no, that's still Django thinking" you've spent $15+ and you're still
manually fixing Django-isms the model smuggled into your FastAPI codebase.

### What happens with stubs

Strip the function bodies. Now the model sees:

- Every endpoint signature (route, params, return type)
- Every model/schema (fields and types, not ORM magic)
- Every dependency (function signatures, not middleware chains)
- The complete API surface — what exists and how it connects

Without Django's implementation details polluting its thinking, the model
writes idiomatic FastAPI: dependency injection instead of middleware,
Pydantic models instead of Django serializers, explicit async queries instead
of lazy QuerySets.

**The bodies were never the spec. The signatures were.**

### The fettling problem

The hardest part of a Django → FastAPI port isn't the initial code generation.
It's the 47 rounds of fettling afterward — discovering implicit Django behavior
that wasn't visible in the code:

- "This view has `permission_classes` but you didn't port the custom
  permission that checks both the user role AND the org subscription"
- "The serializer's `validate_<field>` methods run in a specific order
  that the FastAPI version doesn't preserve"
- "There's a `post_save` signal on this model that updates a denormalized
  counter on another model"

With raw code, the model reads all of this and still misses half of it because
it's drowning in implementation detail. With stubs, the model at least tells
you clearly what it sees and what it doesn't — and each round of fettling
costs 23K tokens instead of 300K.

### We tested it: authentik (1,613 files → one prompt)

[authentik](https://github.com/goauthentik/authentik) is an open-source identity
provider. 1,613 Python files, 153K lines, 1.15M raw tokens. Every Django pattern
in the book: ORM models with inheritance managers, DRF viewsets, Dramatiq tasks,
Django Channels WebSockets, multi-tenancy via schema switching, a custom blueprint
system, RBAC with object-level permissions.

Stubbed: **541K tokens.** Fits in one prompt on any 1M-context model.

We sent the entire stubbed codebase to two models with one instruction: "produce
a detailed, actionable migration plan to port authentik from Django to FastAPI."

| Model | Input tokens | Output tokens | Cost | Time |
|---|---:|---:|---:|---:|
| Gemini 2.0 Flash Lite | 674,090 | 3,822 | **$0.05** | 37s |
| Gemini 2.5 Flash | 674,091 | 6,032 | **$0.10** | 61s |

For **ten cents**, Gemini 2.5 Flash produced a 10-section migration plan that:

- Referenced specific files and classes from the actual codebase
- Identified the strangler fig pattern as the migration strategy
- Called out Django traps: lazy loading, `related_name`, `InheritanceManager`,
  `schema_context` for multi-tenancy, signal ordering, `Meta` class options
- Proposed a 6-phase migration order based on dependency analysis
- Correctly identified the hardest parts: multi-tenancy schema switching,
  RBAC object-level permissions, `ManagedAppConfig` reconciliation
- Recommended keeping Django migrations for schema management while swapping
  the ORM layer underneath

The five-cent model (Flash Lite) went file-by-file — thorough but mechanical.
The ten-cent model produced an architectural plan you could hand to a team.

Neither model needed to read a single function body. The signatures told them
everything: what exists, what depends on what, what Django magic is in play.
Raw code would have cost ~$1.50 (1.15M tokens) and the model would have spent
half its output reproducing Django patterns in FastAPI.

---

## Model outputs (unedited)

### Gemini 2.5 Flash + compressed stubs ($0.005)

> The root cause of the bug lies within the `flask/app.py` file, specifically
> in the `do_teardown_request` and `do_teardown_appcontext` methods. Both methods
> iterate through the registered teardown callbacks, but they execute these
> callbacks in a simple `for` loop. If any callback raises an exception, the
> loop terminates immediately, preventing subsequent callbacks from executing.
>
> **Involved Files and Functions**:
>
> 1. **`flask/app.py`**: `do_teardown_request`, `do_teardown_appcontext`
> 2. **`flask/ctx.py`**: `AppContext` — manages lifecycle, calls the flawed
>    teardown logic
>
> **Fix**: Wrap each individual callback invocation in `try/except`, collect all
> exceptions, execute all callbacks regardless of individual failures, raise a
> composite exception after all callbacks have had a chance to execute.

### Claude Haiku 4.5 + compressed stubs ($0.028)

> The issue is in how teardown callbacks are executed in `flask/app.py`. Both
> `do_teardown_request()` and `do_teardown_appcontext()` methods iterate over
> registered callbacks **without exception handling**, so the **first exception
> aborts the loop entirely**.
>
> **Current problematic pattern:**
> ```python
> def do_teardown_request(self, ctx, exc=None):
>     for func in self.teardown_request_funcs[None]:
>         func(exc)  # ← If this raises, loop terminates!
> ```
>
> **Detailed Interaction**:
> 1. Registration: Blueprints and app register teardown callbacks via decorators
> 2. Storage: Callbacks stored in `self.teardown_request_funcs`
> 3. Execution: During `ctx.pop()`, both teardown methods iterate and call each
> 4. Failure Point: First `func(exc)` that raises causes the iteration to stop
> 5. Result: Remaining callbacks never execute, resources leak

### Claude Sonnet 4.6 + raw code ($0.301)

> The bug is in **`flask/app.py`** in two methods: `do_teardown_request` and
> `do_teardown_appcontext`. Both iterate over registered callbacks without any
> exception handling, so if any callback raises an exception, the exception
> propagates up and aborts the loop — all subsequent callbacks never execute.
>
> In `flask/ctx.py`, `AppContext.pop()` calls both teardown methods inside a
> `try/finally`, but that only ensures `do_teardown_appcontext` runs if
> `do_teardown_request` fails — it does **not** protect individual callbacks
> *within* each method from aborting the others.

**All three models found the same bug and proposed the same fix.** The cheapest
model ($0.005) produced the most detailed analysis. The most expensive ($0.301)
produced an equivalent answer at 60x the cost.

---

## Bottom line

| | Compressed stubs | Raw code |
|---|---|---|
| **Tokens** | 23K (signatures only) | 94K (every line) |
| **Cheapest successful run** | $0.005 (Flash) | $0.301 (Sonnet) |
| **Quality** | Identical | Identical |
| **Speed** | 11s | 28s |
| **Failure rate** | Low (call sites visible) | Higher (buried in detail) |
| **Cost of failure** | N/A | 10-19x single attempt |

Three use cases, same lesson:

- **Bug finding**: the model needs the call graph, not the implementation.
  Stubs show cross-module interactions that raw code buries.
- **Refactoring**: the model needs every call site, not every function body.
  Stubs surface all 277 occurrences; raw code hides 3 test files in 302K tokens.
- **Porting**: the model needs the API surface, not the framework internals.
  Stubs ARE the spec. Raw code teaches the model to reproduce the old
  framework's patterns in the new one.

The model is smart enough. It's just blind. Compression gives it eyes.
