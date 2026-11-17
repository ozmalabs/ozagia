# Ozma Protocol — Whole-Surface Partition

**Status:** Design artifact, feeds a future v6 reorganisation
**Premise:** Split the protocol along the token boundary before tiering anything
**Companion:** [`protocol.md`](./protocol.md) v5.15

---

## Why partition

The v5.x protocol has accreted features whose natural homes are different. Some
features are part of the *token envelope* — the structured blocks the Token
Model reads or emits. Others are part of the *shim backend* — machinery that
runs on the code side and never crosses the token boundary.

Conflating the two has two costs:

1. **Model-unfriendly features on the surface.** HMAC handshakes, dense
   byte-level grammars, and cryptographic hashes live on the token surface in
   v5.x even though no Token Model should be asked to compute them. They exist
   because they lived in the same spec, so they ended up in the same envelope.
2. **Tiering the wrong axis.** Older drafts tiered *everything at once*
   (LE / L0 / L1 / L2 / L3 / L4 mixed surface capability, feature bundle, and
   training state into one ladder). A small Token Model can't do full
   reasoning, but it absolutely can read a retention ring or respect a budget.
   Tiering the backend alongside the surface forced edge models into
   all-or-nothing tiers.

The partition follows one rule:

> **Err on the model side.** Features that require Token-Model-unfriendly
> cognition (hashes, byte math, dense authority grammars, state book-keeping)
> belong in the backend. Features the Token Model genuinely needs to
> participate in belong on the surface.

Once the partition is clean, there is **one surface and one backend**. The
surface is a single interface — the token envelope. There are no separate
EDGE / CORE / FULL interfaces; a deployment just enables a subset of the
envelope's features. The backend is not tiered either — it's a capability
matrix the shim implements (or doesn't) based on deployment.

### The only axis that matters: wire level

Wire level describes the **training state** of the Token Model against the
protocol. It collapses to three tiers, because only three states are
meaningfully distinct:

| Level | What the Model was trained on | What the Model does without help |
|---|---|---|
| **L0** | Stock. Language-first training, no protocol exposure. | Nothing protocol-shaped; shim wraps tool calls in prose. |
| **L1** | **Retrofit.** Language-first Model with protocol support bolted on — inline instruction at request time, or a LoRA / fine-tune patching the Model's behaviour toward protocol fluency. Retrofit depth is a continuous axis inside L1 (prompted → IR-only LoRA → full-protocol LoRA). | Reads / writes whatever envelopes the retrofit has exposed it to; prose fallback for the rest. |
| **L2** | **Native.** Trained from scratch with the token protocol as the first-class training substrate — not language with a protocol retrofit. | The protocol *is* what the Model speaks. No instruction needed, no LoRA patch, no prose fallback. Language happens to come out of some envelopes because training data contains it, not because language is the interface. |

**L0 is stock.** No modification. The shim does all the protocol-shaped work on
its behalf — wrapping tool calls in prose, parsing prose back into envelopes,
running the full hint/check/rule engine as context assembly. The Model sees
English.

**L1 is retrofit.** The Model is language-first, but has been adapted toward
the protocol. Retrofit depth is continuous and a tuning knob for any given
deployment:

- prompted — inline directions at request time
- IR LoRA — fluent on IR, instruction for the rest
- full-protocol LoRA — fluent on most envelopes, prose fallback for the rest
- full-protocol SFT/fine-tune — close to L2 without native pre-training

All of these sit at L1. The shim still does real work (protocol retrofits
don't cover every envelope equivalently), but less than at L0.

**L2 is native.** The Model's pre-training *is* the protocol. Tokens are the
first-class communication interface from step one, not a by-product of
tokenising English. This is the post-"Large Language Model" shape the
programme has been pointing at.

**L2 is a uniform parameter-efficiency gain, not an edge-only trick.** At
every size, an L2 Model outperforms the same size at L0 for protocol-mediated
work, because no parameters are burnt learning a natural language before
learning to communicate. A 0.5B L2 matches stock mid-tier on CPU. A 7B L2
rivals stock frontier-class. A 70B L2 pushes the ceiling of what a Model that
size has been able to do. Edge viability is one downstream consequence of the
efficiency; it is not the motivation.

Saying a Model is L2 implies a massive efficiency benefit and is the cleanest
signal the tier system can carry — which is why wire level is the axis that
earns tier-like language. The retrofit-depth dial inside L1 is an
optimisation parameter, not a tier.

### Feature presets, not surface tiers

EDGE / CORE / FULL are retained as **convenient feature bundles** — presets
a deployment can reference rather than listing capabilities individually.
They are *not* separate interfaces and they are *not* wire-level tiers:

- **EDGE preset** — the minimal useful subset: read code, read outlines,
  emit single-file IR edits. A small L2 Model on CPU typically runs against
  this preset. An L1 Model (especially one with an IR LoRA) can run against
  it too.
- **CORE preset** — the typical subset: reasoning, coordination, perception
  at the symbolic tier, authoring, communicative surface, most capabilities.
- **FULL preset** — the extended subset: formal verification, cross-model
  annotations, raster / streaming perception, specialist dispatch,
  federated / DP markers.

Any wire level can target any preset. L2 hits everything natively; L1 hits
anything via inline instruction or LoRA at higher cost; L0 hits anything via
shim-side prose wrapping. The preset is a deployment choice, not a Model
property.

Everything below about EDGE / CORE / FULL is labelling envelopes with their
preset bundle. Nothing below describes a separate surface or a training-
state tier.

---

## The boundary

```
┌───────────────────────────────────────────────────────────────────────┐
│                          Token Model (component)                      │
│                                                                       │
│                     reads and emits token envelopes                   │
└───────────────────────────────────────▲──────────────────────────────┘
                                         │
                              token boundary (API)
                                         │
┌───────────────────────────────────────▼──────────────────────────────┐
│                          Ozma shim (backend)                          │
│   handshake crypto · state · budgets · routing · grammars · adapters  │
│   hint matching · check runners · telemetry · retention · ACLs        │
└───────────────────────────────────────────────────────────────────────┘
```

Everything *above* the line is the token envelope. Everything *below* is the
shim backend. A feature that has components on both sides (TASK, VISION,
DISPATCH, HANDSHAKE) appears in both inventories, explicitly split.

---

## Part 1 — Token envelope inventory

Every block the Token Model ever reads or emits. Labelled by feature preset
(EDGE / CORE / FULL) for deployment convenience, not as a tier.

**Rule across the whole surface:** the envelope is a typed token contract,
never a language interface. Prose slots (rationales, claims, hints) are
free-form token sequences — the Token Model can fill them with English,
code, IR, emoji, glyph-refs, or any token sequence the envelope accepts.
Language is a choice about *content*, not about the surface. An L2 Model
may emit English inside a rationale slot because its training data
contained English; an L2 Model may equally well emit IR, glyphs, or a
non-human-readable token stream. The envelope does not care.

### Envelope catalogue

#### Core authoring (EDGE-and-up)

| Envelope | Direction | Tier | Notes |
|---|---|---|---|
| **IR** | in/out | EDGE (edge-8 subset) / CORE (full symbol set) | Code blocks in Greek/brace encoding. Edge-8 is the restricted variant; full IR adds the rest of v2 symbols. |
| **EDIT** | out | EDGE (single-file) / CORE (multi-file, transactions) | Position-addressed edits. `fix position §N` prose form available for L0/L1 Models. |
| **OUTLINE** | in | EDGE | Position-indexed code structure. Read-only at EDGE; emitted at CORE. |
| **CODE** (full file) | in | EDGE | Uncompressed source as context input. |

#### Reasoning and justification (CORE)

| Envelope | Direction | Tier | Notes |
|---|---|---|---|
| **RATIONALE** | out | CORE | Per-change prose explanation. |
| **CLAIM** | out | CORE | Invariants in prose, heuristically verified. |
| **CONFIDENCE** | out | CORE | Scalar score on any scoreable attribute. |
| **STRUCTURED-CONF** | out | CORE | Per-aspect confidence map. |
| **REASONING** | out | CORE | Structured thinking / scratchpad surface. |
| **REASONING-PERSIST** | in/out | CORE | Cross-turn reasoning cache references. The *cache itself* is backend; the references are surface. |
| **THINK-ACT** | out | CORE | Interleaved reasoning + tool-call body. |
| **THINKING-MODE** | in/out | CORE | Instant/thinking toggle. |
| **PLAN** | in/out | CORE | Multi-step execution plan with confidence per step. |
| **TEST-SPEC** | out | CORE | Compressed test intent. |

#### Context and structure (CORE)

| Envelope | Direction | Tier | Notes |
|---|---|---|---|
| **HINT** | in | CORE | Confidence-ranked localisation hints injected into context. (Matching is backend; the inserted block is surface.) |
| **GRAPH** | in/out | CORE | Relational structure (deps, call graphs). |
| **TREE** | in/out | CORE | Hierarchical structure (dirs, ASTs). |
| **INTERACT** | out | CORE | Multi-turn context requests. |
| **HANDOFF** | out | CORE | Multi-model context transfer envelope. |
| **MEMORY** | in/out | CORE | Cross-session persistent knowledge references. (Storage is backend.) |
| **PERSONA** | in | CORE | Role/behaviour/emoji_pack/visual_idiom configuration. |
| **AGENT-STATE** | in/out | CORE | Workflow-level goal and progress tracking. |

#### Embeds and aliases (CORE)

| Envelope | Direction | Tier | Notes |
|---|---|---|---|
| **EMBED** | in | CORE | Embedding references as `{@alias}` tokens. |
| **LEARNED-BPE** | implicit | CORE | Repo-specific identifier compression. Tokenizer config is backend; the resolved tokens are surface. |
| **DOCSTUB** | in/out | CORE | Docstring-specific compression. |
| **ATTN** | in | CORE | Attention-aware adaptive expansion markers. |

#### Code-correctness capabilities (CORE)

| Envelope | Direction | Tier | Notes |
|---|---|---|---|
| **MATH** | in/out | CORE | Canonical math blocks. |
| **TENSOR-SHAPE** | in/out | CORE | Named-dimension shape annotations. |
| **SCHEMA** | in/out | CORE | Schema edits with compatibility tracking. |
| **DEP** | in/out | CORE | Dependency operations with CVE context. |
| **EFFECTS** | in/out | CORE | Side-effect tracking (pure/reads/writes/network). |
| **PROMPT** | in/out | CORE | Prompt blocks embedded in Component code. |

#### Communicative surface (CORE)

| Envelope | Direction | Tier | Notes |
|---|---|---|---|
| **STYLE** | in/out | CORE | Communication register/tone/vocabulary. |
| **THEME** | in | CORE | Visual presentation metadata. |
| **PROSODY** | out | CORE | Speech markup for TTS. |
| **VOICE** | in | CORE | Persona voice identity. |
| **AVATAR** | in | CORE | Persona visual identity. |
| **DRAW** (token-native) | out | CORE | SVG / Mermaid / ASCII / glyph-refs. Raster opt-in moves to FULL (see below). |

#### Perception and actuation (CORE)

| Envelope | Direction | Tier | Notes |
|---|---|---|---|
| **VISION** (`structured-only`) | in | CORE | JSON perception (boxes, labels, masks, keypoints, OCR). No pixels cross the boundary. |
| **VISION-ANNOTATE** | out | CORE | Structured visual feedback. |
| **LISTEN** (structured) | in | CORE | ASR/speaker-diarisation as tokens. |
| **SENSOR** | in | CORE | Non-AV sensor streams (summarised). |
| **CAP-PROBE** | in/out | CORE | Capability probing envelope. |
| **MOTION** | out | CORE | Embodied and UI control commands. |

#### Coordination surface (CORE)

| Envelope | Direction | Tier | Notes |
|---|---|---|---|
| **ROUTING-CONTEXT** | in | CORE | Router-visible surface the Model can read. (Routing logic is backend.) |
| **WORK-UNIT** | in/out | CORE | Task structure across three tiers. |
| **DISPATCH** | out | CORE | Model-directed subtask delegation envelope. (Dispatch execution is backend.) |
| **COORDINATION** | in/out | CORE | Peer-to-peer agent messaging. (Queue/retention is backend.) |
| **SCHEDULE** | out | CORE | Scheduling intent. (Scheduler is backend.) |
| **TASK** (spawn/status/result) | in/out | CORE | Tool-call envelopes for long-running tasks. (Lifecycle, budget, wake-on, persistence all backend.) |

#### Wire and instrumentation (CORE)

| Envelope | Direction | Tier | Notes |
|---|---|---|---|
| **WIRE** | in/out | CORE | Compressed prose substitutions. |
| **LOG** | in | CORE | Compressed build/server output. |
| **TRACE** | in | CORE | Compressed stack traces. |

#### Advanced / frontier (FULL)

| Envelope | Direction | Tier | Notes |
|---|---|---|---|
| **FORMAL-CLAIM** | out | FULL | SMT-LIB 2 / Dafny / Z3 / Lean 4 dialects. |
| **POLYGLOT** | in/out | FULL | Cross-language identifier mapping. |
| **COLLAB** | out | FULL | Multi-model gradient-sharing annotations. |
| **FEDERATED** (markers) | out | FULL | Federated-learning aggregation tags. (Aggregation itself is backend.) |
| **DP** (markers) | out | FULL | Differential-privacy budget markers. (Budget accounting is backend.) |
| **GRAMMAR** | in/out | FULL | Grammar semantic versioning. |
| **DRAW** (raster opt-in) | out | FULL | Raster emission via SPECIALIST-DISPATCH path. |
| **VISION** (`inline-full` / `raw-stream`) | in | FULL | Full-resolution and streaming frames. |
| **VISION-EDIT** | in/out | FULL | Streaming region-addressed image editing. |
| **SPECIALIST-DISPATCH** | out | FULL | On-demand specialist model routing. |

### Summary — envelopes by preset

| Preset | Envelope count | Bundle |
|---|---|---|
| EDGE | 4 | Minimum: read code, read outlines, emit single-file IR edits |
| CORE | ~45 | Typical: reasoning, coordination, perception, authoring, communicative surface |
| FULL | ~10 | Extended: formal verification, federated/DP, raster, specialist dispatch |

The count is envelope-shape complexity, not Model-capability complexity. A
retrofitted Model (L1) reaches any preset via inline instruction or LoRA
patching; a native L2 Model speaks any preset directly, because every
envelope was part of its pre-training substrate.

---

## Part 2 — Shim backend inventory

Features the shim implements that never cross the token boundary. Not tiered —
each deployment picks its own matrix.

### Handshake and identity

- **HMAC handshake.** `X-Ozma-Sign` header, shared-secret HMAC-SHA-256. The
  Model never computes or verifies hashes. It sees only the negotiated
  capability set.
- **Capability registry.** Out-of-band declaration of which Components support
  which envelopes. The in-band handshake is a sanity check, not the source of
  truth.
- **Version pinning.** Grammar version negotiation, dialect resolution.

### Authority and access

- **ACL parsing.** `acl:` grammar (principals, ops). The Model emits ACL strings
  as opaque tokens; the shim parses, validates, and enforces.
- **Scope resolution.** `session` / `workspace` / `user` scope routing.
- **Peer control routing.** Cross-runtime handle registry for peer-initiated
  destructive operations (`cancel`, `kill`, `extend_budget`, `resume`).
- **Policy gate evaluation.** OPA Rego, AWS Cedar, Casbin, or
  `policy-as-prompt` gate adapters. The Model sees a gate verdict; the
  evaluation engine is backend.

### State and persistence

- **Retention rings.** Per-scope terminal task rings (session / workspace /
  user). Eviction, publication, and cross-session visibility.
- **Snapshot persistence.** Task state to disk (`~/.ozma/tasks/<scope>/...`),
  PID tracking, subprocess restart.
- **Wake-on evaluation.** Predicate polling for elastic-await. The Model emits
  a predicate; the shim evaluates it.
- **Budget enforcement.** Cost and time budgets, overrun actions
  (`suspend` / `extend` / `escalate`).
- **Cross-session publication.** Workspace-default task surfacing across
  sessions.

### Context assembly

- **Hint matching.** 136+ YAML hints, regex match on input, ~0.02ms per check.
  The Model never sees the detect/guard patterns — it sees only the injected
  guidance block.
- **Check runners.** 15+ output-side validators (regex + external commands:
  ruff, mypy). The Model sees resubmission prompts but not the check engine.
- **Rule engine.** 64+ data-driven rules extending hints/checks. Same
  invisibility principle.
- **Tree-sitter compression.** Stub expansion / contraction. The Model sees
  stubbed code; the AST machinery is backend.
- **Semantic retrieval.** Chroma-backed top-K stub selection. The Model sees
  results; the index is backend.
- **Wire-compression tables.** Substitution applied on ingress/egress; the
  Model sees compressed prose, the table is backend.

### Routing and dispatch

- **Model selection.** `pick_model`, `classify_task`, `estimate_cost`.
  Decisions are backend; the Model sees the results if ROUTING-CONTEXT is on.
- **Dialect adapters.** OpenAI / Anthropic / Google / llama.cpp / vLLM /
  Ollama transport wrappers. The Model sees Ozma envelopes; the adapter
  translates.
- **Specialist dispatch execution.** Router + fan-in for raster, vision,
  verifier, etc.
- **Cost accounting.** Token counting, pricing resolution, budget debits.

### Decode support

- **Constrained decoding.** GBNF / XGrammar grammar compilation and injection
  into llama.cpp / vLLM. The Model decodes under the grammar; the grammar
  plumbing is backend.
- **Edge-8 grammar.** Restricted-vocabulary GBNF for EDGE deployments.

### Perception backend

- **Raster pipeline.** Pixel storage, resolution negotiation, frame
  subsampling, reference-handle resolution. The Model only sees tokens at
  `structured-only`; pixels are an opt-in backend service.
- **ASR.** Speech → text tokens. Whisper/Deepgram/etc. The Model sees tokens.
- **Sensor summarisation.** Time-series → compressed symbolic summaries.

### Coordination backend

- **Agent Message Queue.** `amq-file` / `amq-redis` backends. Maildir-style
  persistence. The Model emits COORDINATION envelopes; the queue is backend.
- **Peer registry.** Known agents, purposes, capabilities.
- **Schedule execution.** Wake-up cron / interval / event triggers. The Model
  emits a SCHEDULE envelope; the runtime fires it.

### Federation backend

- **Federated aggregation.** FedAvg / FedProx / secure-aggregation execution.
  The Model emits FEDERATED markers; the math is backend.
- **DP accounting.** ε / δ budget tracking, Gaussian mechanism noise
  injection. The Model emits DP markers; the noise is backend.
- **Gradient aggregation.** COLLAB multi-model gradient sharing transport.

### Instrumentation

- **Telemetry.** `task.*` event kinds, emission to configured sinks.
- **Audit backends.** `informational` gate routing, compliance logs.
- **Observation.** Latency, cache hit rate, grammar validation stats.

### Subprocess and isolation

- **Subprocess lifecycle.** Spawn, monitor, restart, kill. The Model emits
  `spawn` / `kill` tool calls; the OS machinery is backend.
- **Sandbox enforcement.** Filesystem, network, capability sandboxing. The
  Model emits EFFECTS envelopes declaring intent; the sandbox confines.
- **Handle registry.** Process-level task handle map for cross-runtime routing.

---

## Part 3 — Mixed features (split summary)

A quick index of capabilities that straddle the boundary, and how they split.

| Capability | Surface part | Backend part |
|---|---|---|
| Handshake | Capability list envelope | HMAC compute/verify, key management |
| TASK | `spawn` / status / result tool-call envelopes | Budget, wake-on, persistence, retention, cross-runtime routing, subprocess lifecycle |
| VISION | Structured JSON tier | Raster pipeline, frame handles, resolution negotiation |
| DISPATCH | Dispatch directive envelope | Router, model pool, fan-in, cost accounting |
| COORDINATION | Message envelopes | AMQ backend, peer registry, retention |
| SCHEDULE | Schedule-intent envelope | Trigger runtime, cron/interval execution |
| HINT | Injected guidance block | Pattern matching, rule engine, YAML loading |
| MEMORY | `{@alias}` references | Embedding store, retrieval, TTL |
| EMBED | `{@alias}` references | Embedding store, alias resolution |
| LEARNED-BPE | Resolved tokens in stream | Tokenizer config, repo-specific merges |
| FEDERATED | Aggregation markers | Secure-aggregate backend, gradient transport |
| DP | Budget markers | ε/δ accounting, noise mechanism |
| GRAMMAR | Version envelope | Grammar compilation, GBNF/XGrammar plumbing |
| SPECIALIST-DISPATCH | Dispatch directive | Specialist pool, raster router |
| POLICY gates | Gate verdict envelope | OPA / Cedar / Casbin evaluation |

The pattern: **the Token Model declares intent in tokens; the shim does the
work**. Wherever a capability used to appear to require the Model to touch
bytes, hashes, state, or infrastructure, the partition reveals that only a
small envelope crosses the boundary — the rest is backend.

---

## Part 4 — Custom extensions

Tiers are a cover, not a closed set. Components (the assembly) and shims can
declare **custom capabilities** beyond EDGE / CORE / FULL.

Custom surface capabilities:
- Declared in the capability registry with a custom tag (`x-foo-bar`).
- Grammar contributed as an EBNF fragment (for constrained-decode deployments)
  or as a free-form token block (for unconstrained).
- Handshake advertises the custom tag; stock Token Models ignore unknown tags.

Custom backend features:
- Registered via shim plugin interface.
- Invisible to the Model unless they inject context blocks or receive token
  envelopes.

The partition makes custom extension cheap because the two sides are loosely
coupled: a new backend feature rarely needs a new surface envelope (it
piggybacks on an existing one), and a new surface envelope rarely needs new
backend plumbing (the shim already knows how to parse, route, and audit
envelopes generically).

---

## Part 5 — Implications

A partition this clean has consequences. Listing them here so the v6
reorganisation inherits them explicitly rather than by accident.

1. **L0 / L1 / L2 is the only tier axis.** Wire level describes the Model's
   training state and collapses to three meaningfully distinct states:
   stock, retrofit, native. Retrofit depth (prompted → IR LoRA → full-
   protocol LoRA → full-protocol SFT) is a continuous axis *within* L1, not
   a separate tier. There is no second tier axis on the surface.
2. **EDGE / CORE / FULL collapse to presets, not interfaces.** The protocol
   has one envelope surface. "EDGE" is just the minimal feature bundle; it
   does not live behind a separate interface. A deployment enables whatever
   subset of capabilities it needs.
3. **L2 is the post-"Large Language Model" target.** L0 is stock and L1 is
   retrofit: language-first models being adapted to the protocol. L2 is a
   Model trained from scratch with the token protocol as the first-class
   communication interface. Language is content that can appear in
   envelopes, not the surface of the Model. This is what "moving beyond
   LLMs" concretely means inside the Ozma programme.
4. **L2 is a uniform parameter-efficiency gain.** At every size, L2 beats
   L0 for protocol-mediated work — no parameters are spent on learning a
   natural language before learning to communicate. 0.5B L2 matches stock
   mid-tier on CPU; 7B L2 rivals stock frontier-class; 70B L2 pushes the
   ceiling. Edge viability (L2 on CPU) is one downstream consequence of
   this efficiency, not the motivation. This changes the commercial
   framing from "niche edge deployment" to "universal capability-per-
   parameter win."
5. **Deployments become two decisions, not three.** Wire level and backend
   feature matrix. The envelope preset is not a separate decision — it's
   determined by which backend features the deployment runs and which
   envelopes the Model is trained on.
6. **The shim becomes legible.** Operators enumerate backend features they
   run ("we run hint-match + retention + subprocess + AMQ; we don't run
   federated or DP") without that choice forcing anything on the Model
   beyond which envelopes it must handle.
7. **Comparison tables stop confusing training with interface.** "Any stock
   Token Model is L0" and "any Model with a protocol retrofit (prompt or
   LoRA) is L1" replaces the older all-things-at-once ladder. The retrofit-
   depth dial is a deployment tuning knob, not a new tier.
8. **Natural language stops being load-bearing.** The doc can be written
   purely in terms of token envelopes. Prose is a content choice inside
   free-form slots, not a protocol layer. IR / DRAW / PERSONA /
   constrained-decode all look native to L2 and achievable via shim-side
   help at L0/L1.
9. **Custom is cheap.** Adding a capability means adding one envelope to
   the surface catalogue and (optionally) one backend feature. No tier
   changes; the preset it lands in is just a recommendation.

---

## Open questions

- **Does L2 imply a particular architecture?** Token-first pre-training
  could mean many things — smaller tokenizers, different attention
  patterns, different data curation. For protocol purposes L2 is defined
  by *training substrate*, not architecture. Any architecture that
  pre-trains on token envelopes qualifies.
- **Is the preset label worth keeping at all?** EDGE / CORE / FULL might
  become more confusing than useful once they're no longer tiers. Lean:
  keep them as deployment presets in registry syntax (`preset: edge`) but
  drop them from the capability-per-entry tier column. A capability is
  just a capability; presets are groupings.
- **Where do teachable capabilities sit?** STYLE is marked "teachable:
  true" — L0/L1 Models land at workable compliance with a ~150 token
  teaching prompt. Is that a flag on the envelope or a column? Lean: flag.
- **Capability registry schema change.** Each entry wants
  `{surface: bool, backend: bool, min_level: L0..L2, presets: [edge|core|full]}`.
  The `tier` column becomes `presets` (plural; an envelope can appear in
  multiple presets). `min_level` stays three-valued.
- **How do we surface retrofit depth inside L1?** A Model's L1 retrofit can
  range from "instruction-only" to "full-protocol fine-tune." This is a
  deployment property, not a tier. Lean: a handshake field
  (`retrofit_depth: prompted | ir_lora | protocol_lora | protocol_sft`)
  that the router uses for cost/quality prediction but that doesn't split
  the tier into sub-levels.
- **Deprecated v1.x–v4.x capabilities.** Audit which are still live
  envelopes vs. superseded. HANDOFF may fold into TASK + MEMORY; some v3
  federation markers may fold into v5.x DISPATCH.
- **Does the native L2 tier change the LoRA roadmap?** L2 is the endgame
  but L1 retrofit LoRAs remain the pragmatic path for shipping against
  existing frontier Models. Lean: ship L1 retrofit LoRAs against current
  frontiers; treat L2 as the target for the first full-native Ozma Model.
