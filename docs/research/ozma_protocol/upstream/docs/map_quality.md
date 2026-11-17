# Semantic Map Quality — Forward Specification

**Version:** 0.1  
**Status:** Draft / Forward-looking  
**Companion docs:** [`maps.md`](./maps.md) — Map Format; [`protocol.md`](./protocol.md) — Model Language

---

## Overview

A semantic map is not just a data structure. It is a **designed sign system** — a constructed language a model thinks in. Like any language, a semantic map can be well-formed or malformed, expressive or impoverished, coherent or contradictory.

This document establishes the **quality criteria** for semantic maps: what makes one good, what makes one bad, what can be checked mechanically, and what requires empirical probing. It is intended as a design document for future implementation, not a current requirement.

---

## The nine quality properties

A well-formed semantic map has nine properties. They are not all checkable by the same mechanism, and not all are equally important at build time vs. runtime.

### 1. Internal consistency

**Definition:** No logical contradictions within the map. If `entity A` has property P, and `entity B` inherits from `A`, then `B` also has property P (transitive closure maintained). Combining map elements never introduces undefined or contradictory states.

**Checkable:** Symbolic solver. Represent the map as a constraint system and verify satisfaction. No model involvement required.

**Why it matters:** An inconsistent map produces unpredictable interpretants. The model cannot recover — any inference from an inconsistent premise is unreliable.

**Failure mode:** `map_inconsistent` — two entries make contradictory claims. The map is structurally valid but semantically broken.

---

### 2. Compositional systematicity (Fodor)

**Definition:** If a model can interpret semantic elements `A` and `B` independently, it can interpret their novel combination — even if that combination has never appeared in training. Systematicity is what makes generalization possible.

**Checkable:** Empirical probing. Generate all valid novel compositions from the grammar and test whether the model produces well-typed interpretants for each. Not all compositions need to be semantically meaningful, but all must be grammatically valid.

**Why it matters:** Without systematicity, the map is a lookup table — it covers what it has seen, not what it can handle. The model memorizes, it doesn't generalize.

**Failure mode:** `map_non_systematic` — the map handles seen pairs correctly but fails on novel combinations. Requires retraining or map extension.

---

### 3. Grounded completeness

**Definition:** Every semantic element in the map traces back to something observable or actionable in the world (via the shim). A disconnected subgraph with no path to sensation or action is meaningless — it cannot be grounded.

**Checkable:** Empirically — the map is grounded if for every element, there exists a shim action or observation that exercises it. This requires the shim's capability registry and the map's semantic vocabulary to be co-indexed.

**Why it matters:** An ungrounded element is a floating signifier. It may encode information the model finds internally consistent, but it has no connection to reality. The model's interpretants from ungrounded elements are untethered from consequences.

**Failure mode:** `map_ungrounded` — elements with no shim path. The map is syntactically valid but semantically inert.

---

### 4. Type-correctness (Montague)

**Definition:** Expressions combine according to types. `verb + noun` is well-typed; `noun + noun` without a rule is not. The map enforces grammaticality — well-typed expressions are valid, ill-typed expressions are rejected by construction.

**Checkable:** Type system verification. Given the grammar and the type signatures of all semantic elements, verify that every valid combination in the grammar is well-typed.

**Why it matters:** Without type-correctness, the map admits grammatically invalid expressions. The model must then guess at intent rather than apply deterministic transformation rules.

**Failure mode:** `map_type_error` — the grammar permits an invalid combination. Schema correction required.

---

### 5. Semantic distinctness

**Definition:** Every token in the dialect maps to a distinct semantic element (no synonymy). Every semantic element maps to exactly one token (no ambiguity). The map is a bijection between tokens and meanings at the grammar level.

**Checkable:** Symbolic — check for collisions in both directions. Forward: two tokens with identical semantic content. Reverse: one token used for two distinct semantic elements.

**Why it matters:** Synonymy causes ambiguity at decode time (which token should this semantic element produce?). Homonymy causes ambiguity at encode time (which semantic element does this token represent?). Both destroy round-trip consistency.

**Failure mode:** `map_synonym_collision` or `map_homonym_collision` — must be resolved before the map is used.

---

### 6. Information efficiency

**Definition:** Each semantic element carries the minimum information needed — no redundancy, no weak synonyms that differ only in surface form. The map is compressible without loss.

**Checkable:** Information-theoretic. Measure the mutual information between entries. High redundancy (entries that are predictable from other entries) indicates inefficiency. Remove the redundant entry and check that expressiveness is preserved.

**Why it matters:** Redundancy in the map translates to redundancy in the model's internal representation. An inefficient map wastes `map_budget` on elements that could be derived from others.

**Failure mode:** `map_redundant` — entries that can be removed without expressiveness loss. Cleanup required.

---

### 7. Round-trip consistency

**Definition:** Encoding (semantic element → token) and decoding (token → semantic element) are inverses. The model can round-trip without drift — encode a semantic element, decode the token, recover the original element.

**Checkable:** Empirical. Take a sample of semantic elements, encode them, decode the resulting tokens, compare to originals. Measure the error rate and drift magnitude.

**Why it matters:** If encoding and decoding diverge, the map is not a stable representation. The model's interpretants will accumulate drift over a session. Session state becomes unreliable.

**Failure mode:** `map_drift` — round-trip introduces corruption. Caused by synonymy, homonymy, or type-incorrect composition.

---

### 8. Extensional stability

**Definition:** The map can absorb new semantic elements and new relations without reorganizing the existing structure. New additions do not break existing inferences. The map is monotonic under extension.

**Checkable:** Empirical. Apply a set of valid extensions to the map. Check that all prior inferences still hold and no new contradictions are introduced.

**Why it matters:** A map that requires structural reorganization to add new elements cannot be incrementally extended — every addition risks breaking existing functionality. This is incompatible with session-long map evolution.

**Failure mode:** `map_instability` — adding elements breaks existing inferences. Requires structural revision.

---

### 9. Productivity (Chomskyan)

**Definition:** Finite vocabulary + finite grammar → unbounded expressiveness. New concepts are composable from existing ones, not requiring new atomic tokens. The map is generative, not a memorized chunk repository.

**Checkable:** Grammatical analysis — the grammar must be provably generative (produces an infinite set of valid, well-typed expressions). This is a property of the grammar alone, not the map instance.

**Why it matters:** A non-productive map saturates — as the domain grows, the map requires new atomic tokens for every new concept, leading to vocabulary explosion and poor generalization.

**Failure mode:** `map_non_productive` — the grammar is finite. Requires grammar redesign.

---

## Summary table

| Property | Checkable by | When to check |
|---|---|---|
| Internal consistency | Symbolic solver | Build time |
| Type-correctness | Type system | Build time |
| Semantic distinctness | Symbolic collision detection | Build time |
| Information efficiency | Mutual information | Build time |
| Productivity | Grammar analysis | Build time |
| Round-trip consistency | Empirical test | Build time + runtime |
| Extensional stability | Empirical injection test | Build time |
| Compositional systematicity | Empirical probe | Runtime |
| Grounded completeness | Capability co-indexing | Runtime |

---

## Map quality tiers

Not all maps need to satisfy all nine properties at all times. Different maps serve different purposes:

**Tier 0 — Structural validity** (required)
- The map is a valid instance of its schema.
- Entries are well-typed, scopes are correct, binary format is intact.
- Checked by: schema validator + binary parser.

**Tier 1 — Semantic coherence** (expected)
- The map satisfies: internal consistency, type-correctness, semantic distinctness, round-trip consistency.
- These are the minimum for a map to be usable without surprising behavior.
- Checked by: symbolic solver + round-trip test.

**Tier 2 — Design quality** (aspirational)
- The map additionally satisfies: information efficiency, extensional stability, productivity.
- These make the map maintainable, composable, and durable across generations.
- Checked by: mutual information analysis, injection tests, grammar analysis.

**Tier 3 — Intelligence quality** (research target)
- The map additionally satisfies: grounded completeness, compositional systematicity.
- These are the hardest to measure and require the runtime model to be the test instrument.
- Checked by: empirical probing with the runtime model (see below).

---

## Measurement approaches

### Symbolic (build time, no model required)

**Constraint solver**
- Encode the map as a constraint system (type constraints, scope constraints, composition rules).
- Run a SAT/SMT solver to verify consistency and type-correctness.
- Fast, deterministic, cheap.

**Grammar analysis**
- Extract the production rules from the map's schema.
- Prove (or disprove) that the grammar is generative (Chomsky Type 2 or higher).
- Prove type-safety of all valid productions.

**Collision detection**
- Forward: cluster semantic elements by meaning, flag synonyms.
- Reverse: cluster tokens by mapping, flag homonyms.
- Deterministic, runs on the instance without reference to any model.

**Mutual information analysis**
- Compute pairwise MI between entries.
- Flag entries with MI above threshold (redundant).
- Information-theoretic, requires only the map instance.

**Round-trip test**
- Sample N semantic elements.
- Encode each to token(s), decode token(s) back to semantic element.
- Measure bit-error rate and semantic drift.
- Empirically testable at build time with the same tokenizer the runtime model uses.

### Empirical (build time or runtime, requires model)

**Systematicity probe**
- Enumerate all valid novel compositions from the grammar.
- For each, prompt the model to produce an interpretant.
- Measure grammatical validity and semantic plausibility.
- This is expensive (O(n²) compositions) but the definitive test.

**Extensional stability test**
- Apply a batch of valid extensions to the map.
- Re-run all prior systematicity probes.
- Measure whether existing infererences are preserved.

**Grounded completeness probe**
- For each semantic element, test whether the shim can provide a grounding observation or action.
- Requires the shim's capability registry to be co-indexed with the map's vocabulary.
- The absence of a path for an element is a quality finding, not a schema error.

---

## Map quality errors

When a quality check fails, the map is in one of the following error states:

| Error | Property violated | Severity |
|---|---|---|
| `map_inconsistent` | Internal consistency | Fatal — map unusable |
| `map_type_error` | Type-correctness | Fatal — map unusable |
| `map_synonym_collision` | Semantic distinctness | Fatal — round-trip broken |
| `map_homonym_collision` | Semantic distinctness | Fatal — round-trip broken |
| `map_drift` | Round-trip consistency | Fatal — session state unreliable |
| `map_non_productive` | Productivity | Fatal — map saturates |
| `map_instability` | Extensional stability | Fatal — extensions break existing |
| `map_ungrounded` | Grounded completeness | Warning — semantically inert |
| `map_non_systematic` | Compositional systematicity | Warning — generalization limited |
| `map_redundant` | Information efficiency | Info — maintenance debt |

---

## Relationship to the build process

The nine properties are not just evaluation criteria — they are **design objectives** for the map-builder model.

**Map-maker design target:** A model trained to construct maps should be rewarded for:
- Producing grammars that are type-safe and generative
- Selecting distinct, non-redundant semantic elements
- Maintaining round-trip consistency (encode/decode alignment)
- Avoiding contradictions during incremental construction
- Ensuring every new element has a path to the grounding layer

This is a different training objective from general instruction-following or code generation. The map-maker is a **compiler engineer** — its outputs must be correct by construction, not just plausible in context.

**Self-improvement loop:** A mature build process could use the previous generation's runtime model (trained on map N) to quality-check and refine map N+1 before training the next generation. The runtime model becomes the systematicity probe and grounded-completeness auditor for the next generation's map.

---

## Open questions

1. **How do you measure grounded completeness without a complete shim capability registry?** If the shim is still evolving, elements may be ungrounded only because the shim hasn't implemented the relevant action yet. Map quality is partially a function of shim maturity.

2. **What is the right threshold for information efficiency?** Some redundancy is intentional (robustness, schema evolution). Distinguishing intentional redundancy from waste requires domain knowledge, not just mutual information.

3. **How does systematicity interact with the model's training distribution?** If the model has never seen a novel composition, is the failure a map problem or a training problem? The map can be well-designed but the model's systematicity may need to be trained separately.

4. **Can a map be productive but not systematic?** Yes — a grammar can generate infinite expressions that the model cannot reliably interpret. Productivity is a property of the grammar; systematicity is a property of the model's internalization of the grammar. They must both hold for the map to be fully usable.

5. **What does a Tier 3 map look like in practice?** Grounded completeness + compositional systematicity requires both the shim's full capability registry and the model's ability to generalize across all valid compositions. This is the target state for an L2-native map, not the starting state.

---

## Document history

- **0.1** (2026-04-23) — Initial draft. Properties, checkability, quality tiers, open questions established.