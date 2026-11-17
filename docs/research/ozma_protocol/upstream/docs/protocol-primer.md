# Ozma Protocol — Primer

A short, readable guide to the Ozma wire protocol for LSM–code
interaction. Covers the concepts and mechanisms; skips the formal
grammar, fingerprint tables, and capability-by-capability depth
that live in [`protocol.md`](./protocol.md).

Target audience: someone who wants to understand how Ozma talks to
models in ~20 minutes of reading, and then knows where to dive deeper.

**Foundational axiom:** see [`foundation.md`](./foundation.md). The
protocol treats a model as a machine being instructed, not an entity
being talked to. Everything below is an instruction format.

**Three documents:**
- [`protocol.md`](./protocol.md) — Model Language (invariant envelope grammar)
- [`shim.md`](./shim.md) — Shim Requirements (per-deployment runtime behaviour)
- [`maps.md`](./maps.md) — Map Format (the fundamental data structure everything is built on)

---

## TL;DR

Ozma sits between clients and models as a **shim**. It takes plain
code and text from clients, compresses and structures it into an
efficient wire format, forwards to a model, and decodes the model's
structured output back into plain code and text for the client.

The shim has three **capability levels**: from "stock model, plain
prose in and out" up to "native LSM that speaks every protocol data
type bidirectionally." The level gets negotiated once per session;
the protocol degrades gracefully if the model is less capable than
expected.

Everything the protocol carries — code structure, edits, rationales,
memories, personas, math, schemas, dependencies, side effects —
is addressable by position (`§N`) or stable id (`§N=hash`), so
edits, references, and cross-turn deltas all work cheaply.

The shim is invisible to the client. Clients see plain code; the
model sees compressed, structured context. Compression savings:
50-98% on code, conservative 70%+ on most structured interactions.

---

## The one-paragraph conceptual model

```
┌─────────┐    plain code + text   ┌──────────┐   maps + caps   ┌────────┐
│ client  │ ───────────────────►  │    shim   │ ─────────────► │ model  │
│         │ ◄─────────────────── │  (ozma)   │ ◄───────────── │        │
└─────────┘   plain code + text   └──────────┘  structured     └────────┘
                                                output
```

Clients never deal with the protocol. Models at L2 produce and
consume protocol natively; models at L1 read maps supplied by the
shim and write back in whatever envelopes the retrofit has exposed;
models at L0 see no protocol at all (shim generates prose).

---

## Capability Levels

The core graceful-degradation mechanism. Every feature of the
protocol has a well-defined behavior at each level.

| Level | Name | Map origin | What |
|---|---|---|---|
| **L0** | Stock | None | Plain text in, plain prose out. No map exposure. |
| **L1** | Retrofit | Shim-generated maps | Model reads maps supplied by the shim. May need prompting or a LoRA adapter to become map-literate for a given schema. Writes back in whatever envelope schemas the retrofit has exposed. Prose fallback for unknown schemas. |
| **L2** | Native | Model-generated maps | The model originates maps as output and self-declares the maps it speaks (probe/ack). The protocol is what the Model speaks — map schemas are its native vocabulary. No prompting needed on session start, no prose fallback. |

**Default:** L1 for unknown models. Capability is declared
**out-of-band** via the model registry and confirmed **in-band**
via the handshake.

L1 is the interesting floor: without any special training, any
capable instruction-following model can work with Ozma by following
one-time instructions in its system prompt. L2 is the ceiling —
full bidirectional fluency with millisecond negotiation.

**Map literacy** is the common substrate. L1 and L2 models both
read and write maps. The difference is who originates the map on
this session (shim vs model) and whether the model needs prompting
to understand a new schema (L1) or speaks it natively (L2).

---

## The handshake

When a session starts, the shim announces its capabilities via a
system-message injection (the **probe**), and the model confirms
which it actually supports (the **ack**). A typical probe:

```
[OZMA v5.8 probe:<nonce> caps: OUTLINE:a4f9c1 EDIT:b2c1d3
  INTERACT:d8f3e2 RATIONALE:e1a7f4 ... ]
```

The nonce prevents replay; each capability ships with a fingerprint
(hash of its grammar + semantics) so the model can reject versions
it doesn't know.

Model's first response begins with an ack listing the caps it
actually supports:

```
[OZMA v5.8 ack:<hmac> OUTLINE:a4f9c1 EDIT:b2c1d3 ...]
```

HMAC prevents spoofing. Mismatched or missing caps → shim falls
back to L1 (instructions-only) for those specific types, keeps L2
for the rest.

### Spoofing resistance

Only the first model in a session can ack L2. Mid-session model
swaps (e.g., routing a follow-up to a different model) default to
L1 for the new model unless an orchestrator explicitly reissues the
probe. This prevents a weaker model from inheriting L2 claims.

---

## Self-Upgrade

This is one of the protocol's genuinely novel pieces and worth
understanding even if you skip most of the spec.

### The problem

Suppose we publish protocol v5.9 with a new capability `WIDGET`.
Existing L1 and L2 models can't speak it natively. Waiting for
retraining is months. Waiting for adoption is quarters.

### The mechanism

At session start, the shim can inject a **teaching prompt** that
describes the new capability in natural language with examples:

```
OZMA v5.9 taught you WIDGET. WIDGET emits structured event
descriptors in the form:
  widget: { type: <kind>, id: <§N>, state: <symbol> }
...(examples follow)
```

A capable model at L1 can often follow this description well
enough to produce and consume the new capability correctly, right
now, with no retraining.

### Quality expectations

- Simple structural caps (hints, tags, format-only edits): L1
  models typically reach L2-equivalent quality via teaching.
- Complex caps (formal math, attention-aware expansion):
  teaching lifts L1 partway. True L2 still requires
  training.
- The shim logs teaching success rates per cap per model; under-
  performing teaches get flagged for retraining priority.

### What cannot be prompt-taught

- Structural grammar (constrained decode, handshake format) — must
  be in training; teaching can't bridge the structural gap.
- Cross-session memory with proper decay — needs persistent model
  state.
- Fingerprint/capability handshake — this is the bootstrap layer
  and must be in training.

---

## Session Cache

Redundant content within and across turns is expensive. The
session cache makes it cheap.

### Content-addressable chunks

Every non-trivial block (a file, a stubbed function, a claim set)
gets hashed and assigned a short reference id. Subsequent turns
reference the id instead of re-sending content:

```
Turn 3: "See §6=3f9a1c2d"  ← reference to chunk hashed earlier
```

### Anaphoric references

Small context-carrying markers let the model refer back without
re-quoting:

```
Turn 5 input:  "Continue from §6 with the new requirements."
Turn 5 output: (model-emitted) "Updated §6..."
```

### Cross-turn deltas

Edits ship as compact deltas against the cached version:

```
edit §6 @L24-42: ... replaced body ...
```

Rather than re-sending the whole function.

### External mutation detection

If a file changes outside the session (git pull, user-editor edit),
the shim detects the hash mismatch and forces a re-send of the
changed region. Protects against silent drift.

### Eviction

Least-recently-used by default; configurable per-project. Cached
content is per-session by default; persistent across sessions only
when explicitly flagged (memories, project-state).

---

## Position Addressing

Every structured block the shim emits or the model references is
addressable in two forms:

- **Ordinals**: `§1`, `§2`, `§3` — simple sequence within current context.
  Cheap, minimal tokens, stable within the turn.
- **Stable IDs**: `§1=a1b2c3d4` — short content hash. Survives across
  turns, sessions, and model swaps. Used when a reference needs to
  outlive the current turn.

Example OUTLINE:

```
§1=a1b2c3d4  module app
§2=b2c3d4e5    import os
§4=d4e5f6g7    class Config
§5=e5f6g7h8      def __init__(self, path: str)
§6=3f9a1c2d      def load(self) -> dict
```

Edits can target either form:

```
edit §6 @L24-42: <new body>
edit §6=3f9a1c2d @L24-42: <new body>    // identical effect, outlives turn
```

Parsers must accept either form; strict validation ensures any
referenced `§N` resolves in the current `CodeIndex` (for ordinals)
or the session cache (for stable ids).

---

## Data Types — overview

The protocol defines ~40 data types (capabilities). Each has a
defined grammar, a fingerprint, and an L1-fallback behavior.
Grouping them conceptually:

### Code structure and edits

| Cap | What it carries |
|---|---|
| **OUTLINE** | Position-indexed module/class/function skeleton, ordinal + stable id |
| **EDIT** | Position-addressed code edits (insertions, deletions, replacements) |
| **DOCSTUB** | Docstring-specific compression (preserves function docs while eliding bodies) |
| **ATTN** | Attention-aware adaptive expansion (selectively un-stubs heavy functions) |
| **EMBED** | Embedding-reference aliases (use short tokens for large chunks) |
| **POLYGLOT** | Cross-language identifier mapping (Python `User`↔Go `User` across services) |

**Wire compression** (shim concern, not a cap): The shim applies
corpus-derived substitution maps (keyword aliases, identifier
shortening) transparently. The protocol grammar never sees compressed
symbols. See [`shim.md`](./shim.md) for wire-compression details.

### Reasoning and planning

| Cap | What it carries |
|---|---|
| **RATIONALE** | Per-change explanation — "why this edit" |
| **REASONING** | Structured thinking steps, separable from the answer |
| **PLAN** | Multi-step execution plan (sequence of phases) |
| **CLAIM** | Verifiable invariants the model is asserting ("this preserves signature X") |
| **HINT** | Model-output confidence-ranked localization ("issue is most likely at §6, confidence 0.9") |

### Interaction and collaboration

| Cap | What it carries |
|---|---|
| **INTERACT** | Model asks user/orchestrator a clarifying question |
| **HANDOFF** | Compressed cross-model context transfer (pass a session from model A to model B) |
| **MEMORY** | Cross-session persistent knowledge (facts, preferences, user state) |
| **PERSONA** | Role and behavior configuration (see [`rules-social.md`](./rules-social.md)) |
| **COLLAB** | Multi-model gradient-sharing annotations (for mixture-of-models) |
| **FEDERATED** | Federated-learning aggregation metadata |

### Verification and correctness

| Cap | What it carries |
|---|---|
| **TEST-SPEC** | Compressed test intent (describe what tests should verify) |
| **FORMAL-CLAIM** | Machine-verifiable invariants (stronger than CLAIM; theorem-prover-checkable) |
| **EFFECTS** | Side-effect tracking (reads/writes/network/globals/pure) |
| **SCHEMA** | Schema edits with compatibility tracking (proto, openapi, graphql, sql, avro, json-schema) |
| **DEP** | Dependency operations (add/remove/upgrade/downgrade/pin/audit) |

### Structure and meaning

| Cap | What it carries |
|---|---|
| **GRAPH** | Relational structure (call graphs, data flow, ownership) |
| **TREE** | Hierarchical structure (directory tree, class hierarchy) |
| **MATH** | Canonical math blocks (unicode equations with cross-turn refs) |
| **TENSOR-SHAPE** | Named-dimension shape annotations (the ML-code primitive) |

### Confidence and safety

| Cap | What it carries |
|---|---|
| **CONFIDENCE** | Universal scoreable attribute on any output (0-1) |
| **STRUCTURED-CONF** | Per-aspect confidence maps (structure=0.9, names=0.7, logic=0.6) |
| **DP** | Differential-privacy budget markers |

### Agentic / meta

| Cap | What it carries |
|---|---|
| **AGENT-STATE** | Workflow-level goal and progress tracking, subtasks |
| **TRANSACTION** | Atomic edit groups (all-or-nothing apply) |
| **PROMPT** | Prompts embedded in model-application code (subject to prompt-injection and token-budget verifiers) |
| **GRAMMAR** | Grammar semantic versioning (which grammar version is in play) |

### Presentation

| Cap | What it carries |
|---|---|
| **STYLE** | Communication style (terse, verbose, technical, conversational) |
| **THEME** | Visual presentation metadata (for UIs that render output) |
| **PROSODY** | Speech markup for TTS (pacing, emphasis, inflection) |

### Full cap details

Every cap has its own section in [`protocol.md`](./protocol.md)
with the grammar, input/output examples, L1 fallback behavior,
and fingerprint. This primer summarizes; refer to the spec for
authoritative syntax.

---

## Shim responsibilities

The shim is the pipeline component that owns protocol mechanics.
Full spec lives in [`shim.md`](./shim.md); summary:

1. **Compression** — tree-sitter-stub functions, minify, apply wire-
   compression maps (substitution tables, identifier aliases).
2. **Probe/ack** — issue the handshake on session start; parse the
   ack; downgrade caps that failed negotiation.
3. **Teaching** — inject teaching prompts for novel caps when the
   model's training predates them.
4. **Caching** — content-addressable chunk store; anaphoric ref
   resolution; eviction; external-mutation detection.
5. **Parsing** — validate every structured output against the
   grammar; reject malformed output with a specific error type.
6. **Expansion** — resolve stable ids, splice edit deltas back into
   source files, reverse wire-compression maps.
7. **Compliance adaptation** — gracefully handle partial protocol
   output; log what the model got right and wrong; feed training
   signal back.
8. **Telemetry** — emit structured events for every cap invocation,
   success/failure, token deltas, latency.

The shim is the program. The model is the interpreter. Most bugs
live in the shim, not the model.

---

## System prompt injection by level

What the model sees in its system context depends on its level:

**L0**: no protocol injection. Shim wraps everything in prose.

**L1**: instructional preamble — teaches the model to read maps
supplied by the shim and emit in whatever envelope schemas the
retrofit has exposed. Teaches capability-by-capability when
needed. Size depends on how many schemas are active this session.

**L2**: minimal preamble (~50 tokens) — just a version marker and
any project-specific rules. Model already speaks every cap
natively; no teaching required.

Layering order matters: project rules, persona, current-data
hints, reference hints, prior turn sequence, user's current
message. Detailed in [`provisioning.md`](./provisioning.md) §7.

---

## Pattern compression

For repetitive structures (e.g., "all these 12 files import the
same 8 modules"), the shim emits a **pattern** once and references
it:

```
@pattern-0 { import os, import sys, import json, from typing import List, ...}
§15  file: utils.py  patterns: [@pattern-0]
§16  file: routes.py  patterns: [@pattern-0]
§17  file: models.py  patterns: [@pattern-0]
```

10-20× compression on repetitive codebases (microservices, boilerplate-heavy).

---

## Streaming invariants

The protocol is stream-safe: any data type can be parsed
incrementally as it arrives. Specific invariants:

- Section boundaries (`§N=...`) are line-terminated.
- Grammars never require backtracking past a section boundary.
- Parse errors are localized — one bad `EDIT` doesn't kill the
  rest of the turn.

Means: UI can render partial output (streaming diff preview) as
the model generates; the shim can pipeline expansion with
generation.

---

## Error handling

The protocol defines typed errors for every recognized failure
mode. Examples:

| Error type | Meaning |
|---|---|
| `shape_violation` | Output matches grammar keyword but has wrong structure |
| `schema_compat_mismatch` | SCHEMA edit declared backward-compatible but breaks compat |
| `schema_breaking` | SCHEMA edit is breaking; needs explicit flag |
| `dep_cve_unresolved` | DEP operation doesn't address flagged CVE |
| `effects_contradiction` | `pure=true` declared but output has side effects |
| `prompt_injection_risk` | PROMPT block detected as potentially user-controlled |
| `prompt_token_budget_exceeded` | PROMPT block declared budget exceeded |
| `agent_delta_empty` | AGENT-STATE delta had no fields; shim rejects |

Each error includes the offending section, a reason string, and a
recommended L1 fallback. Errors are *localized*: a single bad
block doesn't kill the whole turn; the shim emits what it can,
logs what it couldn't, and continues.

---

## Telemetry

Every protocol operation emits a structured event:

- Session start/end
- Probe issued, ack received, caps negotiated
- Teaching prompts injected (which caps, per-model)
- Cap parses (succeeded/failed, latency, token counts)
- Cache hits/misses
- Compression ratios per session
- Self-upgrade success rates per (cap, model)
- Degradation events (L2 → L2 due to parse failure)

These feed into training-priority decisions (which caps need
better training coverage), cost analytics, and per-capability
quality dashboards.

See [`protocol.md §Telemetry Schema`](./protocol.md) for the full
event schema.

---

## Capability profiles

Named bundles of caps for different use cases. Not every deployment
wants every cap; profiles let you target:

| Profile | Caps included | Use case |
|---|---|---|
| `ml-code` | OUTLINE, TENSOR-SHAPE, MATH, EDIT, CLAIM, PLAN, CONFIDENCE | ML / data science work |
| `api-code` | OUTLINE, SCHEMA, DEP, EDIT, CLAIM, EFFECTS, TEST-SPEC | Backend API development |
| `research-v3` | Full MATH + TENSOR-SHAPE + FORMAL-CLAIM + REASONING | Research/proof work |
| `agent` | AGENT-STATE + PLAN + HANDOFF + MEMORY + PERSONA | Long-running agent workflows |
| `federated-v4` | FEDERATED + DP + COLLAB | Federated / privacy-preserving training |
| `full-v5` | Everything | Reference deployment |

Profiles reduce handshake overhead (smaller probe); deployments
can add/remove caps without touching the spec.

---

## Model-directed dispatch and work units

Two newer additions (v5.7–v5.8) worth understanding:

### Model-directed dispatch

Instead of Ozma guessing task difficulty from the user prompt and
routing to a tier, **the model itself can emit a dispatch
recommendation** early in its output:

```
dispatch: work_unit=task difficulty=moderate tier=balanced
          estimated_turns=3 parallelizable=false
```

The router reads this and adjusts tiering. Model knows its own
load better than a classifier guessing from prose.

### Work units

Every agent interaction is associated with a **work unit** — a
tracked unit of progress toward a declared goal. Work units have:

- `goal` — the top-level objective
- `subtasks` — decomposed steps
- `progress` — completion markers per subtask
- `estimated_cost` — token / wallclock budget
- `actual_cost` — accumulated

Work units are first-class — they persist across sessions, can be
handed off to other agents, aggregated into project views.
Enables project-level cost analytics, parallel agent dispatch,
and coherent long-running work.

---

## Extension points

The protocol is designed to be extended. Declared extension
points:

1. **New capabilities** — register a cap id + fingerprint + grammar;
   announce in probe; provide L1 teaching prompt. Adoption via
   model registry updates (no global coordination needed).
2. **Session cache backends** — default is in-memory; pluggable
   for Redis, distributed caches, etc.
3. **Custom data types** — domain-specific caps (e.g., medical
   `DRUG-INTERACTION`, legal `CITATION-GRAPH`) extend the base
   protocol without modification.
4. **Telemetry sinks** — structured events can be routed to any
   observability backend (OTLP, Prometheus, cloud logs).
5. **Teaching prompt injection** — self-upgrade mechanism allows
   deploying new caps to already-trained models at session start.

---

## Training data format

For L2-trained models, the spec defines a standard training data
format — each example is an `(input, output)` pair where inputs
are protocol-compressed and outputs are protocol-structured. This
makes L2 training reproducible across organizations.

Covered in [`protocol.md §Training Data Format`](./protocol.md).
Not needed for using the protocol — only for training new L2
LoRAs.

---

## Related docs

| Doc | What it covers |
|---|---|
| [`foundation.md`](./foundation.md) | The axiom: model as instructed machine. Protocol-level conventions derive from this. |
| [`protocol.md`](./protocol.md) | Model Language — every envelope grammar, every error type. ~9,900 lines. Reference document. |
| [`shim.md`](./shim.md) | Shim Requirements — runtime teaching, render backends, IFC, TRACE, session cache, routing. |
| [`maps.md`](./maps.md) | Map Format — the fundamental data structure. Typed, scoped, composable, content-addressed. |
| [`protocol-CHANGELOG.md`](./protocol-CHANGELOG.md) | Full version history from v1.1 to current. Design-decision archaeology. |
| [`provisioning.md`](./provisioning.md) | Rules engine (hints, checks, reference lookup, tool schemas). Sits *on top of* this protocol. |
| [`rules-social.md`](./rules-social.md) | Social / interaction rules — persona, role, traits, proactivity, self-authored rules. |
| [`hints.md`](./hints.md) | Guidance-hint subset user-facing intro. |
| [`hints-vs-prompts.md`](./hints-vs-prompts.md) | Why conditional injection beats always-on prompts. |

---

## Quick orientation: how to read the full spec

1. **If you're integrating a client**: read [`foundation.md`](./foundation.md)
   → this primer → [`protocol.md §Handshake`](./protocol.md) and
   [`shim.md`](./shim.md). Skip everything else.
2. **If you're adding a capability**: read [`foundation.md`](./foundation.md)
   → this primer → [`protocol.md §Extension Points`](./protocol.md) →
   two or three existing capability specs as templates.
3. **If you're training an L2 model**: read the primer, then
   [`protocol.md §Training Data Format`](./protocol.md) and
   [`protocol.md §Reference Encoding`](./protocol.md) in detail.
   Also read [`maps.md`](./maps.md) — the map format is the substrate.
4. **If you're writing hint rules**: start at [`foundation.md`](./foundation.md)
   → [`provisioning.md`](./provisioning.md). The protocol matters
   only insofar as it's the substrate rules are composed for.
5. **If you're evaluating Ozma for your use case**: this primer
   + [`foundation.md`](./foundation.md) is probably enough.
   [`protocol.md`](./protocol.md) is a reference, not a read-through.

---

## One-sentence summary

**Ozma's protocol is a map-native, content-addressable, position-addressed,
capability-negotiated, level-graceful wire format that lets a
pipeline compress code and context for a model, receive structured
output, and splice the results back into source — with typed
errors, observability, and a self-upgrade mechanism to teach new
capabilities to already-deployed models.**

Everything else in [`protocol.md`](./protocol.md) is the specific
grammar and machinery that makes this work reliably at scale.
