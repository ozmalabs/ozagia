# Ozma Map Format Specification

**Version:** 1.0  
**Status:** Draft  
**Companion specs:** [`protocol.md`](./protocol.md) — Model Language; [`shim.md`](./shim.md) — Shim Requirements

This document is one of the **protocol invariants**. It specifies the format of
semantic maps — the fundamental data structure the entire protocol is built on.
All models and shims must speak this format. Everything else (wire-
compression maps, envelope schemas, third-party formats) is an instance of
this format with a specific schema.

---

## What a map is

A map is a typed, scoped, versioned, content-addressed, composable,
bidirectional data structure with a canonical lifecycle. Used for:
- Wire compression (substitution tables, identifier aliases)
- Protocol envelopes (EDIT, CLAIM, REASONING, etc.)
- Schema definitions (what keys exist, what types they have)
- Session state (cached references, user preferences)
- Capability negotiation (handshake fingerprints)

**Examples of map instances with named schemas:** the session cache
(`ozma-session-cache-v1`), MEMORY (`ozma-memory-v1`), POLYGLOT
(`ozma-polyglot-v1`), and user preferences (`ozma-prefs-v1`) are not
separate mechanisms — they are maps with different schemas, cached and
composed by the same engine.

A map has four parts:
1. **Schema** — keys, value types, constraints, composition rules (invariant)
2. **Instance** — concrete values generated from a corpus (shim or model)
3. **Build pipeline** — reproducible steps from raw corpus to validated map
4. **Binary wire format** — compact, cache-friendly, fast to load

### Semiotic model

A map is a **Peircean sign** (representamen): a structured datum that stands
for something else. The **object** is the domain state the map describes — the
code structure, the claim being asserted, the graph topology. The
**interpretant** is the sense the model makes of the map: its internal
representation after processing the sign.

This framing is not philosophical decoration. It has concrete engineering
consequences:

- **The model is an interpreter by construction.** It does not "understand"
  code; it produces interpretants (output maps) from signs (input maps) via
  the schema-governed transformation rules it was trained on. A bug is never
  "the model misunderstood" — it is "the wrong sign was sent, or the schema
  was ambiguous, or the interpretant production rule was not in the training
  distribution."

- **Schema determines interpretant.** The same object (a function definition)
  produces different interpretants depending on which schema the map uses.
  An EDIT-schema map of a function yields edit operations; a GRAPH-schema map
  of the same function yields call-graph edges. The model's behaviour changes
  without any change to its weights — only the schema changes. This is why
  "context bugs not model bugs" is a theorem, not a slogan.

- **The shim grounds objects.** Peirce's triad requires the sign to connect to
  an object in the world. The shim provides this grounding: it reads the
  filesystem (object), produces a map (sign), sends it to the model
  (interpretant production), receives the model's output map (new sign), and
  writes it back to the filesystem (new object). The model never touches
  reality directly; the shim is the indexical layer.

- **Map literacy is interpretant literacy.** A map-literate model can produce
  valid interpretants for any schema it has learned. It does not need to
  re-learn "what a function is" for each new envelope type; it only needs the
  schema that tells it which interpretant production rules to apply.

---

## Three-layer architecture

Maps operate at three layers. A map-literate model (L1 or L2) operates at
the semantic layer by default; the shim supplies the surface and tokenizer
layers on the output path.

### Semantic layer (universal)

Abstract categories that cross languages and domains:
- `function_definition`, `class_definition`, `return_statement`, `import_statement`
- `claim_predicate`, `evidence_reference`, `confidence_value`
- `edit_position`, `edit_old_text`, `edit_new_text`

The semantic layer is the model's native vocabulary. It thinks in these
categories, not surface tokens.

### Surface layer (language-specific)

Bindings from semantic categories to language syntax:
- `function_definition` → `def` (Python), `func` (Go), `function` (JS)
- `class_definition` → `class` (Python), `type` (Go), `class` (JS)

Deterministic for fixed-concept languages. Generated mechanically from corpus
frequency analysis.

### Tokenizer layer (model-family-specific)

Optimizations for the target tokenizer's token boundaries:
- `function_definition` → `d` (Qwen), `ƒ` (Claude), unmapped (GPT-4)
- `return_statement` → `r` (Qwen), `→` (Claude)

Pure cost function. Given a semantic map and a tokenizer vocabulary, select the
shortest token for each semantic class.

**Composition at runtime:**
```
session_map = semantic_base_map + language_surface_map + tokenizer_optimizer_map
```

The model emits semantic tokens. The shim expands through surface → semantic →
tokenizer on the way out. The model reads surface text; the shim compresses to
semantic tokens.

---

## Map schema (invariant)

A schema defines what a valid map instance must contain.

```yaml
schema:
  name: "ozma-edit-v1"
  version: "1.2.3"           # semantic version of this schema family
  fingerprint_algorithm: "sha256"
  scope_levels: [global, file, function, block]

  keys:
    position:
      type: "POSITION"       # reference to another schema
      required: true
      scope: "block"
    old_text:
      type: "string"
      required: true
      max_bytes: 4096
    new_text:
      type: "string"
      required: true
      max_bytes: 4096
    confidence:
      type: "float"
      required: false
      range: [0.0, 1.0]

  constraints:
    - "old_text != new_text"  # simple expression language
    - "position in valid_positions(file_context)"

  composition:
    extends: ["ozma-base-v1"]
    conflicts: ["ozma-edit-v0"]
    merge_strategy: "override"  # how to handle key collisions
```

Schema types:
- `string`, `int`, `float`, `bool` — primitives
- `bytes` — opaque binary data
- `POSITION`, `STABLE_ID`, `HASH_REF` — protocol primitives
- `map_ref` — reference to another map by fingerprint
- `transform` — value is a transformation rule, not a literal
- `conditional` — value has an applicability condition

---

## Map instance

An instance is a concrete populated map following a schema.

```yaml
instance:
  schema_ref: "ozma-edit-v1:1.2.3:a4f9c1"
  fingerprint: "a4f9c1"      # sha256(schema + instance)[:6]
  scope: "block"
  origin: "corpus_sha256:abc123"
  built_at: "2026-04-23T10:00:00Z"

  entries:
    - key: "position"
      value: "§6"
      type: "POSITION"
    - key: "old_text"
      value: "def load(path):"
      type: "string"
    - key: "new_text"
      value: "def load(path: str) -> dict:"
      type: "string"
    - key: "confidence"
      value: 0.95
      type: "float"
      confidence: 0.99           # meta: how sure is the map about this entry?
```

**Entry metadata (per-key):**
- `confidence` — how certain the map originator is about this binding
- `condition` — applicability guard (e.g., "only in Python files")
- `transform` — transformation type: `literal`, `regex`, `reference`, `compose`
- `scope` — where this binding applies: `global`, `file`, `function`, `block`

---

## Binary wire format

Text/YAML is for authoring. The wire format is binary, compact, cache-friendly.

```
[Header] 32 bytes
  - magic: "OZMA\0" (5 bytes) + reserved (1 byte)
  - format_version: uint16 (1 = v1.0)
  - schema_fingerprint: 8 bytes (sha256 prefix)
  - instance_fingerprint: 8 bytes (sha256 prefix)
  - entry_count: uint32
  - flags: uint16 (compression, scope_present, etc.)

[String table] variable
  - string_count: uint32
  - strings: [length: varint, bytes: utf8]

[Entries] variable
  - key_index: varint (into string table)
  - value_type: uint8
  - value_length: varint
  - value_bytes
  - scope: uint8 (optional, from flags)
  - confidence: float16 (optional, from flags)

[Composition table] variable
  - composed_map_count: uint16
  - composed_fingerprints: [8 bytes each]
  - merge_strategies: [uint8 each]

[Footer] 8 bytes
  - sha256(body)[:8] (integrity check)
```

**Properties:**
- Random-access friendly: fixed-size header enables O(1) metadata lookup;
  sorted entries enable binary search.
- Fast hash (content-addressed, body not header)
- Fast diff (sorted keys, shared string table)
- Fast lookup (binary search on sorted key indices)

---

## Composition

Maps compose layerwise. Order matters. Conflict resolution is deterministic.

```
result = base_map + overlay_map
```

**Merge strategies:**
- `override` — overlay wins on key collision. **Default.**
- `append` — values concatenate
- `fatal` — collision is an error, composition fails
- `conditional` — apply overlay only if condition passes

When no strategy is declared, `override` is used. Composed maps may
override this by declaring an explicit strategy in their schema.

**Composition fingerprint:**
`sha256(base_fingerprint + overlay_fingerprint + merge_strategy)[:8]`

A composed map is itself a map. It can be composed again.

---

## Content addressing

**Fingerprint algorithm:**
```
fingerprint = sha256(serialized_schema + serialized_instance)[:8]
```

Serialized form uses the canonical binary wire format, with:
- Header normalized (timestamps zeroed, origin cleared)
- Entries sorted by key
- String table in canonical order (first-seen)

**Map negotiation (handshake):**

The shim advertises active maps in the probe:
```
[OZMA v5.2 probe:<nonce> caps: ... map:a4f9c1,7b3d2e]
```

`map:` carries a comma-separated list of map instance fingerprints the shim
has loaded for this session. The model responds in its ack:
```
[OZMA v5.2 ack:<hmac>
  trained: EDIT:b2c1d3
  map: a4f9c1 understood, 7b3d2e understood
  accept: CLAIM GRAPH
  map_budget: 0.35]
```

**Trained maps** (fingerprint listed after `map:` with `understood`) consume
zero budget. They are internalized.

**Unknown maps** (fingerprint in probe but not in ack) fall back to reference
encoding for that session. The shim logs `map_fingerprint_mismatch`.

**Map selection algorithm.** The shim decides which maps to include in the
probe based on:
1. Session context (language, framework, domain)
2. Model's declared `map_budget`
3. Map complexity costs (see §Map Budget)
4. Cached maps per-workspace and per-deployment

Maps are selected in priority order (semantic base → language surface →
tokenizer optimizer → session-specific) until the cumulative complexity cost
exceeds the model's budget. Remaining needed maps are omitted from the probe;
the session falls back to reference encoding for those.

---

## Versioning

**Schema family version:** Semantic. `ozma-edit-v1` → `ozma-edit-v2` is a
breaking change. New schema family.

**Schema instance version:** Build timestamp or git commit. Not semantic; each
instance has its own fingerprint.

**Map format version:** This spec. `1.0` in the binary header. Breaking changes
to the wire format require a new major version.

---

## Scope resolution

Maps are not flat. A key can have different values at different scopes.

**Scope hierarchy:** `global > file > function > block`

Lookup proceeds from most-specific to least-specific. A `block`-scoped binding
overrides a `function`-scoped binding for the same key.

**Scope in the binary format:** Each entry carries a scope byte. Entries are
sorted by (scope, key) to enable fast range queries.

**Note:** Map scope levels (`global/file/function/block`) govern where a map
entry applies in composition. They are distinct from capability-level scope
fields such as `edit_scope` in the EDIT capability, which governs what
operations a model is authorized to perform.

---

## Delta encoding

Maps evolve during sessions. Deltas are first-class.

```
[map-delta]
  target: "a4f9c1"
  operations:
    - add: {key: "b3", value: "UserAuthenticationService", type: "string", scope: "session"}
    - retire: {key: "b1"}
    - update: {key: "b2", value: "validate_and_refresh_token_v2", confidence: 0.9}
```

Delta fingerprint: `sha256(base_fingerprint + serialized_delta)[:8]`

Applied deltas form a chain. The shim can prune old deltas after snapshotting.

---

## Build pipeline

**Mechanical mode** (deterministic):
1. Corpus ingestion (code files, logs, config)
2. Token frequency analysis
3. Symbol selection (shortest token per semantic class)
4. Collision detection (against AST tokens, string literals, comments)
5. Round-trip validation (compress → expand → original equality)
6. Binary serialization + fingerprint

**Semantic mode** (model-assisted):
1. Classifier model tags corpus tokens with semantic classes
2. Cross-language alignment (Python `def`, Go `func`, JS `function` → `function_definition`)
3. Tokenizer-aware cost function selects optimal symbols
4. Validation against held-out corpus

**Optimizer mode** (per-model-family):
1. Input: semantic map + target tokenizer vocabulary
2. For each semantic class, select the tokenizer's cheapest token
3. Verify no collisions with tokenizer special tokens
4. Output: tokenizer-optimized map

---

## Schema load/unload (protocol envelope)

First-class messages for dynamic schema management.

```
[schema:load fingerprint:a4f9c1 scope:session]
```

The shim loads the schema into the model's working memory. The model's
`map_budget` decreases by the schema's complexity cost.

```
[schema:unload fingerprint:a4f9c1]
```

The shim unloads the schema. Budget freed.

```
[schema:status]
  active: [a4f9c1, 7b3d2e]
  budget_consumed: 0.35
  budget_total: 0.73
```

L2 models emit `schema:status` per-turn or when budget changes.

---

## Map budget

`map_budget` is an opaque float. Model-native scale. Not normalized across
model sizes.

**Registry fields:**
- `map_budget_total` — default floor for L1 (ignored for L2)
- `map_budget_unit` — optional hint: `native` | `tokens` | `relative`

**Handshake:**
```
[OZMA v5.2 ack:<hmac>
  trained: EDIT:a4f9c1
  accept: CLAIM GRAPH
  map_budget: 847.3
  map_budget_unit: native]
```

**Semantics:** The model declares how much new schema complexity it can absorb
this session. The shim selects schemas whose sum of complexity costs ≤ budget.
Overflow falls back to prose.

**Trained schemas** (in `trained:` section) consume zero budget. They are
internalized, always available, trusted.

**Schema complexity cost** is proportional to the schema description length +
examples + validation rules, measured in the model's native units. The shim
learns the mapping from declared budget to actual schemas handled through
empirical interaction.

---

## Self-describing schemas

A schema is itself describable by a map. The "schema schema" defines what keys,
types, and constraints a valid instance schema must satisfy.

This enables bootstrapping: the first map a model learns is "how to read maps."
After that, all other schemas are self-describing instances.

---

## Error codes (map-specific)

| Code | Meaning |
|---|---|
| `map_fingerprint_mismatch` | Handshake map fingerprint not recognized. Fallback to reference encoding. |
| `map_schema_invalid` | Instance does not conform to declared schema. Logged, prose fallback. |
| `map_budget_exceeded` | Schema complexity exceeds declared budget. Schema not loaded. |
| `map_scope_violation` | Entry applied at wrong scope level. Logged, entry ignored. |
| `map_delta_conflict` | Delta operation conflicts with current map state. Logged, delta rejected. |
| `map_composition_failed` | Composed maps have conflicting keys with `fatal` strategy. Logged. |
| `map_corrupt` | Binary format integrity check (sha256) mismatch. Fallback to reference encoding. |

---

## Relationship to other specs

- **protocol.md** — envelope grammars are map schemas. The handshake carries map
  fingerprints. Schema load/unload are standard envelopes.
- **shim.md** — map generation, caching, application, and expansion are entirely
  shim concerns. The protocol grammar never sees compressed symbols.
