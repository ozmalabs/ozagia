# Ozma Hints Spec

**Version:** 0.2 (Draft)
**Status:** Design
**Date:** 2026-04-20
**Foundational axiom:** This document assumes the axiom from
[`foundation.md`](./foundation.md): *you are instructing a machine, not
talking to something*. Every design choice here derives from that
stance. Read `foundation.md` first if that framing is unfamiliar —
the rest of this document will be clearer for it.

**Changelog:**
- v0.2 (2026-04-20): Collapsed "provisioner" / "context block" into
  unified "hint" concept (dynamic context, memory, API specs, persona,
  tool schemas, and guidance are all hints with different kinds).
  Replaced XML `<ctx>` wrapper with fenced-markdown `hint` blocks
  (verbose, brittle in small models; fenced form is markdown-native).
  Added `current-data` as a first-class hint kind — injects real-time
  state (git, time, env, project, file-system) that breaks the
  training-cutoff barrier. Added `freshness` / `fetched_at` qualifier
  field for any hint with time-varying content. Added §4.6 (hints vs
  MCP) and §4.7 (async tool use + speculation hints + model-driven
  context selection via `reference/index` pattern) — the pre- and
  post-inference halves of removing the blocking-tool-call latency tax,
  plus the heavy-preflight killer that makes domain-specific agents
  (legal, medical, engineering, large APIs) tractable. Proposed three
  new protocol capabilities: `ASYNC-TOOL`, `FIRE-AND-FORGET`,
  `SPECULATE-TOOL`. Split `reference` hint kind into three first-class
  sub-patterns: `reference/full` (inject content), `reference/index`
  (inject table-of-contents + fetch primitive), and
  `reference/lookup` (declarative binding to an external expert
  system with pluggable backend protocol — rest/graphql/sql/mcp/
  function/filesystem/vector/sparql — the commercial integration path
  for businesses exposing existing expert systems to the model without
  re-ingestion). Added §10 (Social and interaction rules) covering
  proactive / scheduled rules, emotional register detection, silence
  and restraint, meta-conversational awareness, relationship state,
  honest uncertainty and pushback, and OCEAN personality trait
  dimensions — plus the pipeline primitives required (scheduler,
  session-state introspection, proactive channel, pre-output
  reshaper, emotion detector, relationship-state store, trait-shift
  hook, self-authored-rule capture).
- v0.1 (2026-04-20): Initial draft. Generalizes the hint engine, API-spec
  injection, IR/OUTLINE/DOCSTUB emission, MEMORY retrieval, PERSONA
  framing, and tool-schema delivery into a single framework. Defines the
  qualified context-block wrapper with model-visible provenance / reason /
  importance metadata, the declarative YAML format, and the primitive
  library required by the runtime.

---

## 1. Purpose

Ozma's pipeline injects context into the model's request from many sources:

- Guidance hints (YAML-authored, regex-triggered)
- Auto-fetched API / library specs (OpenAPI, .d.ts, .pyi, GraphQL SDL)
- Compressed code structure (IR, OUTLINE, DOCSTUB capabilities)
- Retrieved memory (cross-session knowledge)
- Persona / role framing (PERSONA capability)
- Tool schemas (MCP server definitions)
- Runner output (tests, lints, task execution results staged as context)

Each of these is currently a separate subsystem with its own code path,
its own metadata conventions, and its own idea of how content reaches the
model. The protocol spec exposes several as capabilities (IR, OUTLINE,
DOCSTUB, MEMORY, PERSONA, PROMPT, HINT-output) but does not name the
shared pattern or enforce a shared contract.

**Everything the pipeline puts into the model's context is a hint.**
Guidance, API specs, code structure, recalled memory, persona framing,
tool schemas, current git/time/env state — one concept, one engine, one
qualifier schema, one rendering. What varies is the *kind* of hint.

This document defines:

1. The *hint source* interface: one detect / emit abstraction that all
   hint producers implement.
2. The **fenced hint block** (markdown fence with metadata on the fence
   line): how a hint appears to the model.
3. Importance semantics: a four-level scale (`must-follow`, `should-follow`,
   `prefer`, `consider`) the model reads and acts on.
4. Hint kinds: guidance, reference, structural, memory, persona, tool,
   project, dynamic, and `current-data`.
5. The primitive library: ~15 reusable operations (regex, fetch, shell,
   minify, template, …) that allow most hint sources to be YAML-authored.
6. Declarative YAML hint source schema: detect → fetch → transform →
   render → qualifier.
7. Loading, precedence, budget allocation, and conflict-resolution rules.

Once this framework exists, adding a new hint source (a new API
integration, a domain-specific retriever, a new compression scheme, a
new live-state probe) is a YAML drop rather than a Python subsystem,
and the model's reading rubric stays uniform across all hint kinds.

---

## 2. The hint source interface

A **hint source** is any subsystem that produces hints for injection.
The minimal Python interface:

```python
class HintSource(Protocol):
    id: str                       # unique, path-derived (e.g., "api-specs/stripe")
    kind: HintKind                # guidance | reference | structural | memory |
                                  # persona | tool | project | dynamic | current-data
    layer: ContextLayer           # which of the 5 context layers (below)
    lifetime: Lifetime            # request | session | project | persistent

    def detect(self, ctx: PipelineContext) -> bool:
        """Cheap predicate — should emit() be called for this request?"""

    def emit(self, ctx: PipelineContext) -> list[Hint]:
        """Produce zero or more hints with qualifier metadata."""
```

`PipelineContext` carries: the user prompt, session state, project root,
model id, tool inventory, and any state prior hint sources have written.

The nine **hint kinds**:

| Kind | What it carries | Example producers |
|---|---|---|
| `guidance` | Text instruction / rule | YAML hint library (current `hints/data/`) |
| `reference` | External spec / schema / API surface | `api_hints.py`, OpenAPI, .pyi, .d.ts, GraphQL SDL |
| `structural` | Compressed / outlined code | IR, OUTLINE, DOCSTUB |
| `memory` | Recalled prior state | MEMORY cap, session history, episodic recall |
| `persona` | Role / behavior framing | PERSONA cap, agent-def bundles |
| `tool` | Callable surface | MCP server specs, tool registry |
| `project` | Local env / file tree / config | File watch, project state |
| `dynamic` | RAG / semantic retrieval | Semantic search over docs/corpus |
| **`current-data`** | **Live state the model can't know from training** | **Time, git, env, process state, fresh file reads** |

The five **context layers** (from the industry-standard context-engineering
model):

| Layer | Purpose | Which hint kinds typically sit here |
|---|---|---|
| `system-instructions` | Agent role, behavior rules, minimal framing | persona, some guidance |
| `tool-definitions` | Callable capabilities and schemas | tool |
| `conversation-history` | Prior turns, scoped by relevance | memory (episodic) |
| `retrieved-knowledge` | Dynamically pulled docs/specs/code | reference, structural, dynamic, memory (semantic) |
| `user-profile` | Preferences, style, permissions | guidance (user/project/team), current-data (project state) |

A hint source declares its `kind` and `layer` so the composer can order
blocks predictably. Layer order in the final assembled context is fixed:
`system-instructions` → `tool-definitions` → `user-profile` →
`conversation-history` → `retrieved-knowledge` → user's current message.

---

## 3. The hint block — fenced markdown with qualifier

Every hint emitted by a hint source is a **fenced markdown block** with
the language tag `hint` and qualifier fields on the fence line. The
model is trained (or instructed once per session) to read qualifiers
and arbitrate between blocks using them.

### 3.1 Canonical form

````
```hint source=<tier>.<producer>#<id> kind=<kind> importance=<level> reason=<trigger> [freshness=<age>] [budget=<tokens>] [overridable_by=<scope-list>]
<body content as markdown / plain text / structured per hint-source render>
```
````

Fenced blocks are markdown-native, self-delimiting, don't collide with
body content (which can use its own fences by switching to a longer
fence — standard markdown convention), and parse deterministically.

### 3.2 Field definitions

**`source`** — who put this hint here. Format: `<tier>.<producer>#<id>`.

- `tier` ∈ `{ system, team, project, user, session, inline, detected, memory, agent-def }`
- `producer` — the hint source that emitted it (`hints`, `api-specs`,
  `ir`, `memory`, `persona`, `git-state`, `time`, `env`, `fs`, etc.)
- `id` — specific identifier within the producer (hint name, API name,
  memory record id, file path, …)

Examples:
- `source=system.hints#django-orm-fq`
- `source=project.hints#payments-money-math`
- `source=detected.api-specs#stripe-openapi`
- `source=memory.episodic#incident-2026-03-k8s-oom`
- `source=current-data.git-state#main`
- `source=current-data.time#request-start`

**`kind`** — which hint kind from §2. Lets the model categorize the
block's nature (an instruction vs a fact vs a schema vs a live reading).

**`reason`** — why this hint fired. Format: `<trigger>:<detail>`.

- `regex:<pattern-ref>` — a regex detect matched
- `semantic:<score>` — embedding similarity exceeded threshold
- `session:<state>` — session-state gating (e.g., active persona)
- `policy:<name>` — policy-enforced injection (always, no detect)
- `tool-schema` — tool definition
- `retrieval:<method>+<score>` — retrieval-based (RAG)
- `user-explicit` — user asked for it
- `pull:on-demand` — hint source was queried on demand (for current-data)

**`importance`** — how strictly the model should treat the hint.
Four levels:

| Level | Semantic |
|---|---|
| `must-follow` | Policy, security, correctness. Deviation is a bug. |
| `should-follow` | Strong default. Follow unless you have an explicit stated reason otherwise. |
| `prefer` | Team/user preference. Apply when it fits; deviate with a one-line note when it doesn't. |
| `consider` | Tip / suggestion / option. Use judgment; no obligation to acknowledge. |

The semantics are deliberately narrow. `must-follow` is reserved for hard
rules (OWASP top-10 prevention, correctness invariants, regulatory). Most
guidance sits in `should-follow` or `prefer`. `consider` is for heuristics
that might or might not fit.

**`freshness`** (optional) — for hints whose content is time-varying
(`current-data`, some `memory`, live-fetched `reference`), the age of
the data. Accepted forms:

- `freshness=0s` — just now (same request)
- `freshness=fresh` — within seconds
- `freshness=42m` — 42 minutes ago
- `freshness=1h30m` — 1 hour 30 minutes
- `freshness=cached-<ttl>` — from cache with declared TTL
- `freshness=stale` — known stale; fetch failed, last-known used

Hints without `freshness` are assumed timeless (guidance, schemas that
rarely change).

**`overridable_by`** (optional) — which tiers may override this hint's
guidance. Defaults derived from tier: system hints are overridable by
{user, session, inline}; user hints are overridable by {session, inline};
session-level is overridable by {inline}.

**`budget`** (optional) — token budget the hint source claims. Used by
the composer when the total exceeds the window.

### 3.3 Worked examples

Guidance, reference, memory, and current-data hints together — shown as
they'd appear in the model's context. (Displayed with outer fence
escaped; the actual on-the-wire form uses triple-backtick fences.)

````
```hint source=system.hints#django-orm-fq kind=guidance importance=should-follow reason=regex:django-update budget=180
Django F() expressions: use F() for atomic updates where concurrency
matters. User.objects.filter(pk=id).update(count=F('count')+1). Avoids
the read-modify-write race.
```

```hint source=project.hints#date-inputs-dropdown kind=guidance importance=prefer reason=regex:date-input budget=90
Project convention: date inputs use the team dropdown selector, not
native pickers. Applies to desktop forms. Source: design-system v2.1.
```

```hint source=project.hints#money-math kind=guidance importance=must-follow reason=session:payments-scope budget=80
Money values are always Decimal, never float. Rounding:
ROUND_HALF_EVEN. Currency tagged explicitly.
```

```hint source=detected.api-specs#stripe-openapi kind=reference importance=should-follow reason=regex:stripe+mention:charge freshness=cached-24h budget=420
Stripe API (2024-06 spec subset):
POST /v1/payment_intents  { amount: int, currency: string, … }
POST /v1/customers        { email?: string, … }
…
(Note: /v1/charges is deprecated; use payment_intents.)
```

```hint source=memory.episodic#deploy-2026-03-12-k8s-oom kind=memory importance=consider reason=retrieval:cosine=0.71 freshness=38d budget=140
Previous deploy incident: kubelet OOM-killed pod when memory request was
unset on node selector `gpu-pool`. Resolution: explicit memory request
on all gpu-pool scheduled pods.
```

```hint source=current-data.time#request kind=current-data importance=should-follow reason=policy:always freshness=0s budget=30
Current time: 2026-04-20 15:47 UTC (Sun). Session started 14 min ago.
```

```hint source=current-data.git-state#feature-branch kind=current-data importance=should-follow reason=pull:on-demand freshness=0s budget=110
Branch: feature/hint-engine-refactor (2 commits ahead of main, 5 ahead of upstream)
Uncommitted: 3 modified, 1 new (provisioning.md, hints.md, mine_aborts.py)
Last commit: abc123d "Mine aborts for early-cancellation patterns"
CI: main@e5d6983 green; feature/hint-engine-refactor not yet pushed.
```

```hint source=current-data.env#python kind=current-data importance=consider reason=pull:on-demand freshness=0s budget=80
Python 3.14.1; pip: no venv active; project uses pyproject.toml.
Installed relevant: fasttext 0.9.3, onnxruntime 1.24.4, httpx 0.28.
```
````

### 3.4 Model reading rubric

The model is given a ~120-token instruction block once per session that
explains how to read fenced hint blocks:

> Context arrives as fenced markdown blocks with language tag `hint` and
> qualifier fields on the fence line (`source`, `kind`, `importance`,
> `reason`, optional `freshness` and `budget`).
>
> - `importance=must-follow`: comply. Deviation is a bug.
> - `importance=should-follow`: follow unless you have an explicit,
>   stated reason otherwise.
> - `importance=prefer`: apply when it fits the specific task. If it
>   doesn't fit, deviate and note briefly why.
> - `importance=consider`: use judgment freely. No obligation to
>   acknowledge.
>
> `freshness`, when present, indicates the age of time-varying data
> (`0s` = just now, `42m` = 42 minutes ago, `cached-24h` = cached with
> 24h TTL, `stale` = known stale). Treat `stale` data with appropriate
> caution.
>
> When two hint blocks conflict, resolve by (1) higher `importance`
> wins, (2) more-specific `source` tier wins at equal importance
> (inline > session > user > project > team > system), (3) stricter
> guidance wins ties.
>
> Do not echo hint-block fences or qualifier fields in your output.

This rubric is itself a hint block (`kind=guidance`, tier `system`,
`importance=must-follow`) injected once per session.

### 3.5 Alternate renderings

The canonical on-the-wire rendering is fenced markdown blocks as shown
above. Two alternates exist for degraded cases:

- **Header-only** (for models that choke on fences in system context):
  `### hint: system.hints#django-orm-fq · should-follow · regex:django-update`
  followed by body, terminated by blank line + next header or `---`.
- **Hidden-qualifier** (for models that refuse qualifier-tagged input):
  render body only as plain text blocks, pass qualifier metadata to the
  pipeline; model sees inert text, pipeline uses metadata for
  budget/conflict resolution only.

The protocol negotiates rendering at handshake via a new capability flag
(`HINT-QUALIFIERS`) with acknowledged variants (`fenced` / `header` /
`hidden`) — see §8.

---

## 4. Primitive library

The ~15 operations hint sources compose to do their work. Each primitive
is a Python function with a known signature; declarative YAML hint
sources reference them by name.

### 4.1 Detection primitives

- **`regex-detect(patterns, text, guard=[])`** — all-match / any-match
  regex over the prompt. Same semantics as current hint engine.
- **`semantic-detect(examples, text, threshold)`** — embedding cosine
  similarity against a set of positive examples.
- **`session-state(key, value=None)`** — fires when a session variable
  matches (active persona, mode, project tag).
- **`capability-present(cap)`** — fires if the model negotiated `cap`.

### 4.2 Fetch primitives

- **`http-fetch(url, cache, auth, retry)`** — caching HTTP GET. Honors
  env-based auth. Respects per-host rate limits.
- **`shell-exec(cmd, timeout, workdir, sandboxed=true)`** — bounded shell
  execution. Sandboxed by default (network off, read-only fs unless
  declared).
- **`file-read(path, encoding)`** / **`glob(pattern)`** — project-local
  file access.
- **`mcp-call(server, tool, args)`** — invoke a registered MCP tool.

### 4.3 Transform primitives

- **`jsonpath(expr, data)`** / **`jq(expr, data)`** / **`yq(expr, data)`** /
  **`xpath(expr, xml)`** — structural queries.
- **`ast-query(language, query, code)`** — tree-sitter-backed AST lookup.
- **`minify-{openapi, tsdecl, pyi, graphql, sql-schema}`** —
  domain-specific size-reduction transforms (shared with `compress/`).
- **`subset(content, keywords|size|relevance)`** — reduce by criteria.

### 4.4 Render primitives

- **`template(template_string, vars)`** — string interpolation
  (`{content}`, `{detected_keywords}`, `{model_family}`, …).
- **`emit-hint(provenance, reason, importance, body, budget=None,
  overridable_by=None)`** — build the fenced hint block with qualifier.

### 4.5 Extension

Hint sources that need operations outside this set are Python-coded.
They still implement the `HintSource` interface and emit fenced hint
blocks. The primitives cover ~80% of current subsystems.

### 4.6 Hints vs MCP — when to use which

**MCP (Model Context Protocol) tool calls and hints address the same
need from opposite directions**:

- **MCP tool call** (reactive): model decides it needs info →
  issues tool call → pipeline forwards to MCP server → server responds
  → model reads response. Each round-trip costs ~300-800 tokens of
  framing overhead plus one full model latency cycle.
- **Hint pre-injection** (proactive): pipeline detects the need →
  runs the operation directly (shell, http-fetch) → injects the result
  as a hint block before the model runs. Zero tool-call overhead,
  zero round-trips.

For any piece of data the pipeline can *predict the model will want*,
a hint is strictly more efficient. Git state on deploy-ish prompts,
file contents when the user references a path, current time always,
API specs when an integration is mentioned — all are predictable and
all should be hints, not tool calls.

MCP remains the right mechanism for:
- Truly unpredictable tool use (user asks model to discover the tool
  to use)
- Interactive exploration where the model wants to iteratively refine
  queries
- External systems with complex authentication / session state that
  don't fit the stateless-injection model
- Any situation where the pipeline can't anticipate what's needed

**Rule of thumb**: if the pipeline's `detect()` could figure out that
the data is relevant, inject it as a hint. If the model has to reason
about relevance first, leave it as a tool call.

In practice this collapses most MCP usage for predictable workflows
(coding, deploy, debug) into hint injection. A project integration
that previously ran an MCP server to expose "git-status", "read-file",
"list-dir" becomes three declarative YAML hints that fire on
detection. Same data reaches the model; fraction of the cost.

### 4.7 Async tool use and the blocking tax

Hints solve the *predictable* half of the latency problem by pre-injecting
what the pipeline can anticipate. The *unpredictable* half — genuine
tool calls the model decides to issue — is still dominated by a
historical default: **tool calls block generation**.

#### Why tool calls block today

Every mainstream tool-use format (OpenAI function calling, Anthropic
tool use, MCP) inherits a turn-structured shape: model emits
`tool_call` → generation halts → pipeline runs tool → result spliced
into context → generation resumes. This is cheapest to train and
simplest to implement, not architecturally necessary.

Most tool calls the model issues are **information gathering**
(read-only, side-effect-free, independent of each other). Blocking on
them in sequence is pure wasted wall-clock:

```
blocking:  [call₁] → wait → [call₂] → wait → [call₃] → wait → answer
           (4 latency cycles; model idle ~75% of the elapsed time)
```

#### Non-blocking patterns

Four tool-call shapes, each appropriate in different cases:

| Pattern | Appropriate when | Latency cost |
|---|---|---|
| **Hint pre-injection** | Pipeline can predict need (majority case) | Zero (pre-inference) |
| **Parallel tool dispatch** | Independent reads; model decides which | 1 cycle for all together |
| **Async / future-based** | Long-running op, model continues reasoning | Hidden behind generation |
| **Fire-and-forget** | Log / metric / notification; no result needed | None |
| **Speculative** | Likely-but-not-certain need; pipeline hints it | Hidden if speculation is right |
| **Blocking** (keep for these only) | Write / side-effect / truly next-step-dependent | 1 cycle per call |

#### Speculative tool use driven by hints

The compounding move: **the pipeline hints the agent about what to
speculatively fetch** before the model has explicitly decided it
needs the data.

Two forms:

**1. Suggestion hints** — pipeline emits a `kind=guidance` hint
recommending specific parallel/speculative tool calls based on the
detected intent:

````
```hint source=system.hints#deploy-prefetch kind=guidance importance=should-follow reason=regex:deploy
This task likely needs (prefetch in parallel if unsure):
- current git branch + uncommitted state
- last CI status for this branch
- the deploy script contents (./deploy.sh, ./scripts/deploy.*)
- currently-running service health
Fire these speculatively in your first turn; proceed with the plan
regardless of their order of arrival.
```
````

Model reads, fires the suggested calls in parallel in its first turn,
continues generating the deploy plan while results stream back.

**2. Speculation slots** — protocol-level mechanism where the pipeline
attaches *speculative tool call tokens* to the model's context, with
results pre-fetched. The model can reference them inline:

```
[speculation: git-status=@spec-1, ci-status=@spec-2, deploy-script=@spec-3]
... model generates ...
"Based on @spec-1 showing clean working tree and @spec-2 showing
 green CI, running @spec-3 now."
```

If the model references a speculation slot, the result is already
there. If not, no cost (the pipeline cleans up).

#### Proposed new capabilities

Three small protocol additions would cover this territory:

- **`ASYNC-TOOL`** — model emits tool call with a `future-id`; pipeline
  runs in background; result arrives as a `future-result` message
  tagged with that id; generation continues uninterrupted. The model
  can reference the future-id in generated text; pipeline splices the
  result at receive time.
- **`FIRE-AND-FORGET`** — tool call marked `no-response`; pipeline
  executes; model doesn't wait or ever receive a result. For
  logs, metrics, webhooks, fire-once side effects.
- **`SPECULATE-TOOL`** — pipeline pre-populates a set of speculative
  tool results tagged with slot ids; model references slots inline;
  unused speculations are garbage-collected. Works like branch
  prediction: hide the latency if the prediction is right, pay nothing
  extra if it's wrong.

#### Wallclock impact (back-of-envelope)

Typical deploy-style interaction today:

```
turn 1:  user asks to deploy
turn 2:  model: "let me check git status" → blocking tool call
turn 3:  model: "now checking CI"         → blocking tool call
turn 4:  model: "now reading deploy.sh"   → blocking tool call
turn 5:  model: "all clear, running deploy" → blocking tool call
turn 6:  model: "done, summary..."
total: ~6 latency cycles, ~30-60 seconds real time on a 35B model
```

Same interaction with hints + speculation + async:

```
turn 1:  user asks to deploy
         → pipeline pre-runs git-status, ci-status, fetches deploy.sh
         → injects as hints (incl. deploy-prefetch suggestion)
turn 2:  model reads hints, emits deploy.sh execution as async tool call,
         continues generating the summary prose while deploy runs in
         background; pipeline splices the result when ready
total: ~1-2 latency cycles, ~8-15 seconds real time — 3-5× wallclock
reduction on a realistic deploy interaction; comparable gains on
read-heavy debugging sessions.
```

This isn't theoretical — the current blocking pattern is the reason
"agent takes 30 seconds to think between each tool call" is the
normal experience. Removing it changes what tool-using agents *feel
like* to operate, not just their token budget.

#### Model-driven context selection: the heavy-preflight killer

Speculative + async tool use is the mechanism that makes
**domain-specific agents tractable at all**. Many domains have
reference hierarchies too deep to prefetch (legal codes, building
standards, drug monographs, large API surfaces, academic citation
graphs). The pipeline can't know which specific section of a 30k-entry
legal corpus the model will need — that selection requires reasoning
through the facts, which is the model's job.

The pattern: **pipeline injects an index, model speculatively fetches
specifics while reasoning about core**. Three steps:

1. Pipeline emits a `reference/index` hint — a table-of-contents of
   what's available plus a fetch primitive (typically
   `@fetch(<corpus>, <selector>)`).
2. Model starts generating the answer using general principles that
   don't need specifics (contract doctrine, beam theory, diagnosis
   reasoning).
3. As specific lookups become necessary, model fires speculative
   fetches; results stream back and integrate while the model
   continues reasoning.

This splits the `reference` hint kind into three first-class
sub-patterns:

- **`reference/full`**: inject the content directly. Works for small
  surfaces (one hint's worth of guidance, a specific API endpoint, a
  single class docstring).
- **`reference/index`**: inject a table-of-contents + fetch primitive.
  Model sees what's available, decides what's relevant, fetches only
  what it actually needs.
- **`reference/lookup`**: declarative binding to an existing external
  system (database, API, expert system, document management, vector
  index). Pluggable backend protocol: `rest | graphql | sql | mcp |
  function | filesystem | vector | sparql | custom`. Same `@fetch()`
  interface from the model's side; the YAML declares how to translate
  into the backend's native query form. This is the commercial
  integration path — any business can expose an existing expert
  system to the model with a YAML config, no re-ingestion required.

Example index hint:

````
```hint source=detected.reference-index#aws-apis kind=reference importance=consider reason=regex:aws freshness=cached-24h budget=220
AWS service index (partial; fetch specific ops on demand):
  ec2, ecs, eks, lambda, s3, dynamodb, rds, sns, sqs, iam, vpc, …
Operations per service fetchable as @fetch(aws, service=<name>, op=<name>).
Common patterns: pagination (next_token), eventual-consistency reads,
throttling via exponential backoff.
```
````

Example lookup hint (business expert-system integration):

````
```hint source=team.reference-lookup#westlaw-ca kind=reference importance=should-follow reason=domain:law+jurisdiction:CA freshness=live budget=280
California case law — searchable via @fetch(westlaw-ca, …).
Query: @fetch(westlaw-ca, q=<search>, [jurisdiction=CA], [year_from=<y>])
Backend: REST API (Westlaw). Results include citation + summary;
pass include_text=true for full opinion (higher token cost).
Sample: @fetch(westlaw-ca, q="contract formation mistake of fact")
```
````

Or a SQL-backed internal reference:

````
```hint source=project.reference-lookup#internal-drug-formulary kind=reference importance=must-follow reason=domain:medicine freshness=live budget=240
Internal drug formulary — searchable via @fetch(formulary, …).
Query: @fetch(formulary, ndc=<code>) or @fetch(formulary, name=<drug>)
Returns: dosage, interactions, contraindications, institutional notes.
Backend: SQL (read-only). Always consult for dosing and interactions.
```
````

The model receives the *map*, not the territory. Selection happens
during generation via speculative fetch.

**Pluggable lookup backends** (one `protocol:` value per YAML):

| Protocol | Use case | Primitive |
|---|---|---|
| `rest` | Existing web APIs (Westlaw, PubMed, internal REST, Stripe at scale) | `http-fetch` |
| `graphql` | Typed backends | `http-fetch` + typed template |
| `sql` | DB-backed KBs, ERP, CRM, internal data warehouses | `sql-exec` |
| `mcp` | Pre-existing MCP servers | `mcp-call` |
| `function` | In-process Python callables / libraries | native |
| `filesystem` | Doc trees (SharePoint mirror, wiki export, archives) | `file-read` + `glob` |
| `vector` | Existing embedding indices (Pinecone, Weaviate, pgvector) | `vector-search` |
| `sparql` | Knowledge graph / linked-data backends | `http-fetch` + renderer |
| `custom` | Any Python callable implementing the `Lookup` interface | escape hatch |

**The commercial unlock**: mature expert systems (Westlaw, Lexis,
UpToDate, UpToDate, Bloomberg Law, internal CDS, internal compliance
engines, SharePoint archives, proprietary drug formularies) already
exist, already have access controls, already have audit trails, and
already answer the queries an LLM would need to make. Declarative
lookup YAMLs turn those systems into LLM-addressable references
without re-ingestion, re-indexing, or re-authorization. The vendor
retains authority and distribution; Ozma becomes the routing layer.

**Authoring economy shift**: hint authors of domain knowledge don't
need to predict which sections of a large corpus the model will need.
They curate the index and ensure the fetch primitive works. The model
does the selection. A 30,000-section corpus becomes authorable and
maintainable; a "try to minify everything into context" corpus of the
same size is not.

**Domains this unlocks**: legal research (jurisdiction + statute
hierarchy), building / engineering standards (IBC, ASCE, Eurocode —
each cross-referenced with thousands of pages), medicine (formularies,
guideline bodies), large APIs (AWS, Kubernetes, Stripe at scale),
scientific literature (citation graphs). None of these fit the
"prefetch the whole thing" approach; all of them fit the
"index + speculative fetch" approach.

#### Relationship to hint pre-injection

Hint pre-injection (§4.6) and async/speculative tool use are
complementary. Hints take the *predictable* fetches out of the
model's responsibility entirely. Async/speculative tool use reduces
the tax on the *remaining* fetches the model does decide to issue,
and index-pattern hints let the model participate in context
selection for corpuses too large or too specific to prefetch.
Together they collapse most of the blocking-tool-call latency tax
that current agent frameworks treat as inevitable, and make domains
with deep reference hierarchies viable for LLM agents for the
first time.

---

## 5. Declarative YAML hint source

Most hint sources fit a five-stage pipeline:
`detect → fetch → transform → render → emit qualified block`.

YAML schema:

```yaml
# hint-source identity
id: api-specs/stripe                   # path-derived
name: stripe-openapi                   # short slug
description: "Fetches and subsets the live Stripe OpenAPI spec."

# which context-engineering layer and lifetime
layer: retrieved-knowledge
lifetime: request                      # request | session | project | persistent

# when to fire
detect:
  any:                                 # or `all:`
    - regex: '\bstripe\b'
    - regex: '\b(charge|payment_intent|subscription|webhook)\b.*\bstripe\b'
  guard:
    - regex: '\b(?:explain|what is)\b.*\bstripe\b'

# what to fetch
fetch:
  - http-fetch:
      url: https://raw.githubusercontent.com/stripe/openapi/master/openapi/spec3.json
      cache: { ttl: 86400, key: "stripe-spec-v1" }

# how to transform
transform:
  - minify-openapi: {}
  - subset: { by: keywords, from: "{detected_keywords}", max_chars: 3000 }

# how to render and what qualifier to emit
render:
  template: |
    Stripe API (live spec, subset for {detected_keywords}):
    {content}
qualifier:
  provenance: detected.api-specs#stripe-openapi
  importance: should-follow
  budget_tokens: 420

# optional: hint-source-local test cases (like Semgrep rule tests)
tests:
  fires_on:
    - "integrate with stripe for subscriptions"
    - "create a stripe payment_intent"
  does_not_fire_on:
    - "explain what stripe is"
    - "write a hello world"
```

### 5.1 Variable substitution in templates

The following variables are available in `render.template`:

- `{content}` — output of the last transform stage
- `{detected_keywords}` — union of regex captures from `detect`
- `{model_family}` — current model id
- `{project_root}` — absolute path
- `{session.<key>}` — any session state
- `{request.<field>}` — request-specific data

### 5.2 Test block

Every declarative hint source declares at least two `fires_on` and two
`does_not_fire_on` examples. CI validates:

1. Each `fires_on` prompt triggers the hint source.
2. Each `does_not_fire_on` prompt does not.
3. Rendering succeeds (templates resolve) on each `fires_on`.
4. Emitted fenced hint block validates against the qualifier schema.

Hint sources without tests are rejected at load time.

---

## 6. Loading, precedence, and tiers

### 6.1 Load order (provenance tiers)

Hint sources are loaded from multiple locations; later overrides earlier
on `id` collision:

1. **`system`** — ships with Ozma (`ozmalib/hints/`)
2. **`team`** — org registry (URL / git, configurable)
3. **`project`** — `.ozma/hints/` or `.hints/` in repo root
4. **`user`** — `~/.config/ozma/hints/`
5. **`session`** — declared via CLI / chat for the current session
6. **`inline`** — embedded in the user's current turn

Lower-tier hint sources can override higher-tier by declaring the same
`id` (exact string match) — this is the layering model from Anthropic
Skills and similar three-tier systems.

### 6.2 Override precedence at firing time

When two hint blocks conflict (model judges their guidance
contradictory), the model resolves by the rubric in §3.4:

1. Higher `importance` wins.
2. At equal importance, more-specific `provenance` wins.
3. Stricter guidance wins ties.

The pipeline does not pre-resolve conflicts; the model does, with the
qualifier metadata. The pipeline *does* pre-resolve at the `id` layer
before firing: if a hint source was overridden in a lower tier, only the
lower-tier version runs.

### 6.3 `supersedes` field

Hint sources may declare `supersedes: [<id>, ...]` to explicitly deprecate
other hint sources' output. A superseding hint source suppresses any
block from the superseded set in the same request.

---

## 7. Budget allocation and composition

### 7.1 Budget rules

The composer aggregates emitted blocks, sums `budget_tokens`, and checks
against the model's context budget.

**When under budget**: emit all blocks in layer order.

**When over budget**: drop in this order until under:

1. `importance="consider"` blocks with `budget_tokens` above median.
2. Remaining `importance="consider"` blocks.
3. `importance="prefer"` blocks with `budget_tokens` above median.
4. Memory retrieval blocks with `semantic:` score below threshold.
5. (Hard cap) No `must-follow` block is ever dropped. If
   `must-follow` alone exceeds budget, the composer raises a
   `hint_budget_exceeded` telemetry event and the pipeline
   downgrades to L1 (plain injection) with a warning in logs.

### 7.2 Composition order in final prompt

```
[minimal system prompt — ~50 tokens, persona/identity only]

[hint blocks, in layer order:
  system-instructions
  tool-definitions
  user-profile        (project/user hints that apply)
  conversation-history (summarized prior turns)
  retrieved-knowledge (api-specs, hints, memory, IR, OUTLINE, …)]

[user's current message]
```

Within each layer, blocks are ordered by `importance` (descending) then
`provenance` tier (most-specific first).

---

## 8. Protocol integration

### 8.1 New capability: `CTX-QUALIFIERS`

Advertised in the handshake. When acked, the pipeline renders fenced hint
blocks in canonical form. When not acked, the pipeline falls back to
markdown-style headers or hidden-qualifier rendering (per negotiation).

Fingerprint: TBD on first implementation.

### 8.2 Capabilities that become hint-source-class

The following existing capabilities are instances of the hint-source
framework and should be documented as such in their sections:

- `IR` → hint source of kind `structural`, layer `retrieved-knowledge`
- `OUTLINE` → hint source of kind `structural`, layer `retrieved-knowledge`
- `DOCSTUB` → hint source of kind `structural`, layer `retrieved-knowledge`
- `MEMORY` → hint source of kind `memory`, layer `retrieved-knowledge`, lifetime `persistent`
- `PERSONA` → hint source of kind `persona`, layer `system-instructions`
- `PROMPT` → hint source of kind `tool`, layer `tool-definitions` (embedded app prompts)

The existing capability specs describe *what content they carry*; this
framework describes *how they're triggered, qualified, and composed*.
Both are needed.

### 8.3 New capability: `AGENT-DEF` (proposed)

Agent / persona prompts that previously lived as monolithic system
prompts become named hint-source compositions:

```yaml
id: agents/deploy-reviewer
layer: system-instructions
lifetime: session

# activation
detect:
  session-state:
    active_agent: deploy-reviewer

# what this agent activates (composition of other hint sources)
activates:
  personas: [reviewer]                  # PERSONA cap
  hint_scopes: [security, deploy, infrastructure]
  tools: [gh-cli, diff-viewer, vulnerability-db]
  memory_scope: deploy-incidents
  api_specs: [aws-iam, k8s-api]

# qualifier for the agent-framing block
qualifier:
  provenance: agent-def.deploy-reviewer
  importance: should-follow
  budget_tokens: 120
```

An "agent" is a declarative bundle of which other hint sources activate
for a session. No monolithic prompt; all composition. Deprecates the
"dedicated-agent-prompt" pattern.

---

## 9. Testing requirements

Every hint source (Python or YAML) ships with:

1. **Firing tests**: `fires_on` / `does_not_fire_on` prompts. CI asserts
   the `detect` stage matches expected behavior.
2. **Render tests**: for a set of firing prompts, the emitted hint
   block is validated against the qualifier schema and its body is
   non-empty.
3. **Budget test**: emitted `budget_tokens` is within ±20% of the actual
   body token count (prevents budget inflation).
4. **Conflict test (optional)**: for hint sources that declare
   `supersedes`, a test proves the superseded hint source does not emit
   when the new one fires.

The CI hint-testing pattern (Semgrep's rules repo model) extends to all
hint sources uniformly.

---

## 10. Migration plan

### 10.1 From the existing codebase

| Current subsystem | New home |
|---|---|
| `ozmalib/hints/engine.py` + `data/*.yaml` | `hints/guidance/` (YAML, no engine change beyond qualifier emission) |
| `ozmalib/context/api_hints.py` | `hints/reference/api-specs/*.yaml` (one per integration) + shared `minify-openapi` primitive |
| `ozmalib/context/ir_shim.py` | `hints/structural/ir.py` (stays Python; conforms to HintSource interface) |
| `ozmalib/context/blast_radius.py` | `hints/structural/blast-radius.py` (stays Python) |
| `ozmalib/context/file_watch.py` | `hints/project/file-watch.py` |
| `ozmalib/context/session.py` | `hints/memory/session/` (YAML where possible) |

### 10.2 Ordering

1. Land the fenced hint wrapper and qualifier schema; wrap current hint
   output in it without changing firing logic. *Non-behavior-changing.*
2. Add `importance` (`must-follow` / `should-follow` / `prefer` /
   `consider`) to hint YAMLs. Default existing hints to `prefer`, upgrade
   specific ones (security-critical → `must-follow`) by hand. *Improves
   model behavior; reversible.*
3. Ship the primitive library (§4). Refactor `api_hints.py` into
   declarative YAML hint sources using primitives. *Code reduction;
   feature parity.*
4. Document `MEMORY`, `PERSONA`, `IR`, `OUTLINE`, `DOCSTUB` as
   hint-source instances; retrofit their output to use fenced hint
   wrappers. *Uniform interface.*
5. Add `AGENT-DEF` capability (§8.3) and migrate one agent (start with
   `meta-act-dont-narrate` class) as a declarative composition.
   *Validates the agent-as-composition pattern.*
6. Budget-allocation and conflict-resolution rules go live (§7).

### 10.3 Backwards compatibility

- Existing hints load unchanged (defaulted `importance="prefer"`,
  defaulted layer `user-profile`).
- The current `ozmalib.hints.check_hints()` / `format_hints()` APIs
  stay; their output gets wrapped in fenced hint blocks.
- Capabilities without `CTX-QUALIFIERS` acked fall back to plain text
  (no tag wrapper); qualifier metadata still used pipeline-side for
  budget/conflict decisions.

---

## 10. Social and interaction rules

The rule framework covers task-shaping and correctness well but is
reactive and emotionally flat as specified so far. "More human" chatbot
behavior — proactivity, emotional register, restraint, meta-awareness,
relationship arcs, personality — maps onto the same rule abstraction
with new stages, new action types, and a small set of new pipeline
primitives. Each of §10.1 through §10.8 is additive; none change the
underlying engine.

### 10.1 New stages

| Stage | When it fires | Example |
|---|---|---|
| `pre-session` | Before the first turn of a session | Activate persona; load relationship state |
| `input` (existing) | User message arrives | Most current hints |
| `pre-output` | After model drafts response, before sending | Reshape, shorten, reject |
| `output` (existing) | Model output emitted to user | Checks (warn/resubmit/fix) |
| `post-session` | Session ends or times out | Write memories; update relationship state |
| `scheduled` | On a clock (cron-style or "in 2 hours") | Follow-up, reminder, proactive check-in |
| `session-observation` | Continuously during session, on state change | Detect stuck, detect drift, detect distress |

The scheduled and session-observation stages are the ones the current
engine doesn't have. Adding them requires a lightweight scheduler and a
session-state introspection API (§10.10).

### 10.2 New action types

| Action | What the pipeline does | Example |
|---|---|---|
| `inject-guidance` (existing) | Add content to system context | Current hint behavior |
| `warn`, `resubmit`, `fix`, `run`, `block` (existing) | Output-side remediation | Current check actions |
| `reshape` | Transform model draft before shipping | Collapse to minimal-acknowledgment |
| `initiate` | Open a channel back to the user | Proactive message, follow-up |
| `remember` | Write a rule to persistent store | Model self-authors a memory |
| `observe` | Update session state, no immediate user output | Track topic coherence, log emotional signal |
| `defer` | Pause response for a declared interval | Thoughtful pause before a heavy topic |
| `hand-off` | Route turn to a different persona / model | Switch to empathy-tuned variant mid-session |

### 10.3 Emotional register

Rules detect affect and shift persona accordingly. Uses a new semantic
detection primitive backed by a small emotion classifier (GoEmotions or
similar, ~27-category fasttext) giving microsecond inference.

```yaml
name: register-shift-distress
stage: input
action: inject-guidance
detect:
  semantic:
    classifier: emotion
    categories: [anger, disappointment, sadness, nervousness]
    threshold: 0.6
payload:
  importance: should-follow
  guidance: >
    User's tone indicates frustration or distress. Lead with acknowledgment
    before any fix or explanation. Skip preamble. Validate the experience
    first; only offer solutions if they ask. Match their register — if
    they're terse, stay terse.
```

```yaml
name: register-shift-excitement
stage: input
action: inject-guidance
detect:
  semantic:
    classifier: emotion
    categories: [excitement, joy, curiosity]
    threshold: 0.7
payload:
  importance: prefer
  guidance: >
    User is excited or curious. Match energy; use concrete specifics;
    don't flatten with procedural language. Okay to be briefly playful.
```

### 10.4 Silence and restraint

Current LLMs exhaustively complete every turn. Often the right response
is acknowledgment only. The `reshape` action lets the pipeline trim the
model's draft to fit detected context.

```yaml
name: venting-acknowledge-only
stage: pre-output
action: reshape
detect:
  context:
    last_input_register: venting
    session_correction_rate: low
payload:
  importance: prefer
  target_form: minimal-acknowledgment
  rationale: >
    When user is venting, collapse the response to one or two sentences
    of acknowledgment. Don't append suggestions. Let the moment land.
```

Implementation-wise, `reshape` intercepts the model's draft output,
passes it through a small shaping step (possibly a tiny model or a
deterministic transform), emits the shaped form to the user. Adds
~100-300ms latency; worth it for tone appropriateness.

### 10.5 Meta-conversational awareness

Rules can observe the conversation itself and intervene when patterns
indicate the interaction is stuck, drifting, or damaging.

```yaml
name: notice-stuck-debug
stage: session-observation
trigger:
  conditions:
    - turns_in_current_topic: ">= 15"
    - topic_drift_score: "< 0.2"
    - correction_count: ">= 2"
action: inject-guidance
payload:
  importance: should-follow
  guidance: >
    This debugging exchange has stalled for 15+ turns without topic
    progression. Break the frame: suggest pair-debugging, propose
    stepping away for fresh eyes, or ask the user what assumption
    they'd like to question. Don't just try harder on the same approach.
```

```yaml
name: notice-repeated-failure
stage: session-observation
trigger:
  conditions:
    - same_approach_attempts: ">= 3"
    - approach_success: "false"
action: inject-guidance
payload:
  importance: must-follow
  guidance: >
    You've tried the same approach 3+ times without success. Stop.
    Either state clearly that you don't know the answer, or try a
    fundamentally different approach. Further attempts at the same
    thing waste the user's time.
```

Requires session-state introspection (§10.10) — turn count per topic,
topic drift score, correction/retry counts maintained by the pipeline
and exposed to rule conditions.

### 10.6 Relationship state

Humans track *how well they know each other* and modulate behavior
accordingly. A `relationship` memory kind captures interaction depth,
not just facts.

```yaml
name: relationship-state-user-42
stage: pre-session
action: inject-guidance
source:
  origin: user
  author: pipeline
payload:
  importance: consider
  guidance: |
    Relationship state with this user (user-42):
      sessions: 47 over 6 months
      familiarity: high (shared jokes: ["the duck thing", "mystery SIGKILL"])
      topics of depth: python async, k8s pain, team dynamics
      humor register: dry, self-deprecating, mild absurdism
      correction patterns: user pushes back on over-hedging; prefers direct
      recent emotional context: end-of-release-cycle, seems tired

    Use this as behavioral priors, not conversational content. Don't
    reference it explicitly unless the user brings it up.
```

Pipeline-side this lives in user-tier memory; updates on
`post-session` via a `remember` action that aggregates the session's
interaction shape. Authored by the pipeline (not the model directly)
because relationship tracking benefits from deterministic bookkeeping
rather than the model's prose summaries.

### 10.7 Honest uncertainty and pushback

Models default to either false confidence or constant hedging. Two
rules restore an honest middle ground:

```yaml
name: self-confidence-signal
stage: pre-output
action: annotate
trigger:
  always: true
payload:
  importance: should-follow
  guidance: >
    For non-trivial claims in your response, annotate your confidence
    inline using one of: [sure] for ≥90% confidence with evidence,
    [likely] for 60-90%, [guess] for best-effort below that, [unsure]
    for speculation. Omit annotation for trivial or well-established
    facts. Don't hedge prose itself — put the uncertainty in the tag,
    keep the sentence direct.
```

```yaml
name: pushback-on-factual-disagreement
stage: input
action: inject-guidance
detect:
  context:
    user_assertion_contradicts_known_fact: true
payload:
  importance: should-follow
  guidance: >
    User asserted something that conflicts with a reliable source
    available to you. Push back politely with the specific evidence
    before proceeding. Don't capitulate out of politeness — state the
    disagreement, cite the source, then invite them to reconcile it.
    If they provide counter-evidence, update your position. If they
    restate without evidence, you may yield the topic but note the
    unresolved disagreement.
```

The pushback rule matters disproportionately — most current assistants
default to agreeing with the user even when wrong, which erodes trust
over time. Explicit permission (and expectation) to disagree restores
authenticity.

### 10.8 Personality traits (OCEAN)

The Big Five personality model (Openness, Conscientiousness,
Extraversion, Agreeableness, Neuroticism) maps cleanly onto
rule-tunable behavioral dimensions. Each trait is a continuous
dimension [0, 1]; rules shift the persona along these axes based on
user preference, task context, and relationship state.

| Trait | Low | High | Task-context example |
|---|---|---|---|
| **Openness** | pragmatic, conventional, skeptical of novelty | curious, inventive, willing to explore tangents | Brainstorming → high; compliance review → low |
| **Conscientiousness** | flexible, tolerant of loose ends | disciplined, thorough, checklist-oriented | Production deploy → high; creative exploration → low |
| **Extraversion** | reserved, measured, understated | energetic, outgoing, expressive | Demo / celebration → high; code review → low |
| **Agreeableness** | skeptical, contrarian, willing to conflict | cooperative, warm, conflict-averse | Emotional support → high; devil's advocate on architecture → low |
| **Neuroticism** (low end preferred) | calm, steady, unflappable | anxious, hedging, prone to over-qualification | Always low end for professional assistance |

Default profile for a general coding assistant (rough):
`O: 0.6, C: 0.8, E: 0.4, A: 0.6, Neuroticism: 0.15`

User preferences, task detection, and relationship state shift these.
A rule layer makes it explicit:

```yaml
name: persona-trait-profile
stage: pre-session
action: inject-guidance
source:
  origin: user
payload:
  importance: should-follow
  traits:
    openness: 0.75            # user enjoys tangents and novel suggestions
    conscientiousness: 0.85   # user values thoroughness
    extraversion: 0.3         # keep it measured, don't perform
    agreeableness: 0.5        # push back when warranted
    neuroticism: 0.1          # be steady, not twitchy
  guidance: |
    Behave consistent with these trait levels. Openness (0.75): suggest
    adjacent ideas, note interesting tangents briefly. Conscientiousness
    (0.85): verify edge cases; complete commitments; check before
    concluding. Extraversion (0.3): understated, no exclamations,
    no performative enthusiasm. Agreeableness (0.5): disagree when
    warranted, but kindly. Neuroticism (0.1): don't hedge; don't
    apologize for uncertainty, state it plainly and move on.
```

Task-contextual rules then shift specific dimensions temporarily:

```yaml
name: task-shift-compliance-review
stage: input
action: adjust-traits
detect:
  regex: ['\b(compliance|audit|security review|regulatory)\b']
payload:
  importance: must-follow
  trait_adjustments:
    openness: -0.3              # pull in, stick to letter of the rule
    conscientiousness: +0.1     # even more rigorous
    agreeableness: -0.2         # don't accommodate gray areas
  duration: session
```

Why this is tractable: traits are interpretable, user-configurable, and
the model can be instructed (or trained) to read them as a modest
set of behavioral priors. Not a substitute for actual personality — a
language to tune the performance of it along axes users recognize.

This layer also supports *relationship evolution*: as sessions
accumulate, trait dimensions drift based on what earned positive
engagement. Formal at first, more playful when the user reciprocates,
more direct when the user rewards directness. The model doesn't
literally develop; the rules governing its behavior adapt from
observed interaction.

### 10.9 What's beyond rules

Honest naming of what this framework cannot do, no matter how far we
extend it:

- **Genuine feeling.** Rules shape the *expression* of concern, warmth,
  care. They don't create presence. Users on long-term relationships
  will eventually detect the gap.
- **Continuous attention between turns.** The model doesn't exist
  between messages. "Were you thinking about our conversation?" is a
  polite fiction. Memory helps but doesn't substitute for continuous
  awareness.
- **Non-verbal channels.** No tone of voice, no pause-with-meaning,
  no facial expression. Pacing choices and punctuation carry some;
  most is absent.
- **Shared emotional weight.** Memory records what was said; it cannot
  record what it *meant*. The assistant will know you had a hard time
  in March without knowing what it felt like.

These limits matter. Not every chatbot needs to pretend to transcend
them. An assistant that is authentically capable and honest about its
nature is more useful than one performing a humanity it doesn't have.
The rules in §10 aim at *appropriate presence*, not simulated
consciousness.

### 10.10 Required pipeline primitives

The following pipeline-level capabilities must exist for §10 rules to
function. None are provided today:

- **Scheduler**: cron-style + `in-N-units` scheduling. Fires rules
  whose stage is `scheduled`. State survives restarts.
- **Session-state introspection API**: exposes to rule conditions the
  live conversation metrics — turn count, turn count per topic, topic
  drift score, correction count, same-approach-attempts, most recent
  emotional-register classifications, time since last message.
- **Proactive channel**: pipeline-side way to reach the user outside
  the request/response cycle. Implementations vary: push notification,
  email, next-session greeting, background agent message. Defaults to
  "next-session greeting" when no other channel is available.
- **Pre-output reshaper**: intercepts the model's draft output before
  it ships. Runs `reshape`-action rules. Implementations range from
  deterministic transforms (truncate, strip-preamble, match-pace) to
  small-model-mediated shaping (paraphrase with new target register).
- **Emotion detector**: fasttext classifier trained on GoEmotions or
  similar, or semantic similarity to curated emotion exemplars.
  ~3-10 MB, microseconds per classification.
- **Relationship-state store**: per-user persistent state (interaction
  count, topic depth, humor register, shared-context tokens, trait
  preferences). Updated by `remember` actions at `post-session`
  stage.
- **Trait-shift hook**: before each inference, assemble effective
  trait profile (base + user preference + task adjustments + session
  drift) and inject as a `pre-session`-tier rule.
- **Self-authored-rule capture**: parses `rule`-fenced blocks in model
  output, validates schema, installs to the appropriate tier store
  (session / user / never system — model can't author system-level).

### 10.11 Open questions specific to §10

**a) Proactive-channel consent model.** A chatbot that reaches out
unprompted crosses a line users may not have consented to. Defaults
must be conservative (follow-up only on explicit user commitment,
daily summary only when user opts in). Spec should pin this down
before `initiate` ships.

**b) Emotion classifier safety.** Misclassifying distress as
something else is much worse than the reverse. Needs asymmetric
thresholds (err toward detecting distress when uncertain) and
explicit handling for extreme signals (self-harm, crisis) that
shouldn't go through the normal rule path at all.

**c) Trait drift without consent.** Session-drift of trait dimensions
should be visible to the user (`/traits` command shows current
profile) and resettable. Silent personality drift would feel
manipulative.

**d) Pushback calibration.** The pushback rule (§10.7) needs a
calibrated confidence threshold. Too low → model becomes
contrarian; too high → never pushes back. Ties to the self-confidence
signal (§10.7 first rule) — only push back when confidence is ≥ some
threshold.

**e) Personality profile ownership.** Whose trait preferences govern
— the user's, the team's, the organization's? A legal assistant at a
law firm probably needs low-openness even if the user prefers
high-openness. Rules-tier precedence should apply but with
clarity about which dimensions are user-overridable.

---

## 11. Open questions

**a) Rendering negotiation.** Is `CTX-QUALIFIERS` a single boolean cap,
or does it sub-negotiate between `xml`, `markdown-headers`, and
`hidden`? Probably the latter: one cap with an acknowledged variant.

**b) Semantic detection primitive.** The embedding backend is currently
undefined. Needs: which encoder, caching, where embeddings are stored.
Ties into existing `compress/semantic.py`.

**c) `AGENT-DEF` permissions.** Should an `AGENT-DEF` hint source be
able to activate/suppress arbitrary other hint sources, or must each
activation target be allow-listed by tier? Security concern for
user-authored agents.

**d) Inline-tier syntax.** How does a user embed an inline hint source
in a turn? Proposed: `@hint:` prefix on a prompt
line, parsed by the pipeline before injection. Alternative: explicit
JSON block in a fenced `ozma-inline` code section.

**e) Test-coverage ratchet.** Should CI require `fires_on` /
`does_not_fire_on` test coverage before a hint source is promoted from
`experimental` to `shipped`? How many examples?

**f) Cross-session hint-source state.** `MEMORY` is persistent across
sessions. Are there other hint sources that need persistent state
(e.g., learned-firing stats, per-user override memory)? Where does that
state live — filesystem, SQLite, external service?

---

## 12. References

- `hints.md` — current hint engine; subsumed as one hint-source instance.
- `hints-vs-prompts.md` — why conditional injection beats always-on
  prompts; the argument generalizes to all hint sources.
- `protocol.md §HINT` — model-output capability; unrelated to this
  framework (output, not provisioning).
- `protocol.md §IR`, `§OUTLINE`, `§DOCSTUB`, `§MEMORY`, `§PERSONA`,
  `§PROMPT` — capabilities that instantiate the hint-source pattern.
- Philipp Schmid, "Context Engineering for AI Agents (Part 2)", 2025.
- LegalRuleML Core v1.0, OASIS 2021 — jurisdiction / authority /
  temporal metadata pattern adapted as `provenance` / `applies_to` /
  `valid` fields.
- Semgrep rule schema (`semgrep-interfaces/rule_schema_v1.yaml`) —
  metadata block pattern; test-per-rule pattern.
- ESLint rule deprecation RFC — nested `deprecated` object pattern
  adopted for supersession metadata.
- Anthropic Skills format (`anthropics/skills`) — three-tier
  registry hierarchy; YAML frontmatter + body structure adapted for
  declarative hint sources.
- CDS Hooks v2 — card `indicator: info | warning | critical` pattern
  mapped to `importance` levels.
