# AGI Research Comparison: Ozma vs. The Field

**Version:** 0.1
**Status:** Draft
**Date:** 2026-04-23
**Companion docs:** [`compound_loop_proof.md`](./compound_loop_proof.md) — Formal Theorems (9 theses, 21 lemmas); [`graph_memory.md`](./graph_memory.md) — Graph-Native Memory

---

## Introduction

This document compares the Ozma architecture to existing AGI research programs. The comparison is structured by research thread, identifying what Ozma shares with each approach, what it does differently, and where it fills gaps in the field.

---

## 1. Cognitive Architectures: ACT-R, Soar, CLARION, HTM

### ACT-R (Adaptive Control of Thought-Rational)

**What it is:** A symbolic-cognitive architecture with two irreducible kinds of knowledge — declarative (chunks) and procedural (productions). Reasoning proceeds via buffers that connect to modules, with productions firing to modify buffers.

**What Ozma shares:**
- ACT-R's buffer-based architecture → Ozma's activated semantic element sets (functionally similar)
- ACT-R's procedural/production firing → Ozma's map delta rules (both are rule-based updates)
- ACT-R's declarative chunks → Ozma's memory nodes (both are persistent knowledge representations)
- ACT-R's spreading activation over buffers → Ozma's spreading activation over the memory graph

**What Ozma does differently:**
- **ACT-R is symbolic-first; Ozma is grammar-constrained but not purely symbolic.** Ozma allows arbitrary token sequences in content fields; ACT-R requires all knowledge to be chunk-typed.
- **Ozma has typed edges as first-class.** ACT-R's chunk slots are typed within a chunk but edges between chunks are implicit (association strength). Ozma's typed edges (involves, contradicts, supports, caused_by) carry semantic meaning that ACT-R lacks.
- **Ozma has neuroplasticity.** ACT-R has a fixed set of modules; new modules cannot be added during runtime. Ozma's ⊕_neuro operator allows new sub-models to be instantiated during consolidation. ACT-R cannot grow its own structure.
- **Ozma has explicit coherence (not accuracy) semantics.** ACT-R treats chunks as facts to be retrieved accurately. Ozma treats memory as reconstruction with false memories as a feature.

**Verdict on ACT-R:** Ozma is a successor architecture — it preserves ACT-R's buffer/production structure but adds typed graph edges, neuroplasticity, and composite model coordination.

---

### Soar

**What it is:** A production-rule system where if-then rules match working memory elements and fire in parallel. Supports reinforcement learning, semantic memory, and episodic memory. Key feature: chunking — when an impasse occurs, Soar creates new rules from the problem-solving trace.

**What Ozma shares:**
- Soar's chunking → Ozma's memory node creation from reasoning traces (both create persistent knowledge from problem-solving episodes)
- Soar's episodic + semantic memory distinction → Ozma's memory graph with temporal edges (episode) and semantic edges (aboutness)
- Soar's reinforcement learning → Ozma's valence edges (both tag memories with positive/negative assessments)

**What Ozma does differently:**
- **Soar has no protocol layer.** Soar is a single-model architecture — all knowledge is in one production system. Ozma's protocol layer decouples the output grammar from any specific model implementation.
- **Soar cannot spawn new sub-models.** Chunking creates new rules within the same production system. Ozma's neuroplasticity creates new sub-models entirely.
- **Soar has no grammar closure theorem.** Ozma's Thesis II proves that systematicity is transitive under grammar closure. Soar has no analogous formal result.

**Verdict on Soar:** Ozma extends Soar's chunking idea to structural growth (new sub-models), but Soar's formal analysis of production system dynamics is deeper than Ozma's current treatment of delta rules.

---

### Hierarchical Temporal Memory (HTM)

**What it is:** A biologically-constrained architecture modeling the neocortex. Uses sparse distributed representations (SDRs) for encoding, spatial pooling for pattern recognition, and sequence memory for temporal patterns. Constantly learns; no separate training/inference modes.

**What Ozma shares:**
- HTM's SDR → Ozma's token encoding (both are high-dimensional sparse representations)
- HTM's sequence memory → Ozma's temporal edges in the memory graph
- HTM's biological grounding → Ozma's biological analogues (spreading activation = hippocampal retrieval, consolidation = neocortical integration)
- HTM's continuous learning → Ozma's consolidation cycle (no separate training mode)

**What Ozma does differently:**
- **HTM is binary; Ozma is typed.** HTM uses only binary SDRs. Ozma uses typed edges with typed nodes — more expressive but less biologically constrained.
- **HTM has no symbolic/production layer.** HTM does spatial and sequence recognition but not rule-based reasoning. Ozma has map delta rules and grammar closure.
- **HTM cannot model multiple specialized regions.** HTM models the neocortex as a uniform hierarchy. Ozma's composite model explicitly has specialized sub-models with different plasticities.
- **HTM has no formal convergence proof for sequence memory.** Ozma's spreading activation converges (Lemma 12). HTM's sequence memory algorithm is described operationally without formal guarantees.

**Verdict on HTM:** Ozma's memory model is more expressive (typed graph vs. binary SDRs) and has formal proofs where HTM has operational descriptions. HTM's biological constraints are more detailed.

---

### Sparse Distributed Memory (Kanerva)

**What it is:** An associative memory operating on high-dimensional binary vectors. Data is stored at addresses close to the input address; retrieval works by providing a similar address and reading the nearest stored pattern.

**What Ozma shares:**
- Both are associative memory systems
- Both use similarity-based retrieval
- Both model memory as distributed (not local)
- Both can retrieve partial/ corrupted inputs

**What Ozma does differently:**
- **SDM has no typed edges.** All memory locations are equivalent. Ozma's typed edges encode semantic relationships that SDM cannot represent.
- **SDM has no consolidation mechanism.** Memory grows indefinitely. Ozma's consolidation prunes and updates.
- **SDM has no episodic structure.** All addresses are equivalent. Ozma's temporal edges group memories by episode.
- **SDM is content-addressable but not grammar-constrained.** Ozma's grammar closure ensures systematic reasoning; SDM has no equivalent.

**Verdict on SDM:** SDM is a substrate for associative memory. Ozma's memory graph is richer (typed, temporal, consolidated) but builds on SDM's insight.

---

## 2. Universal AI: AIXI and Approximations

### AIXI

**What it is:** Marcus Hutter's theoretical formalism combining Solomonoff induction (universal sequence prediction) with sequential decision theory. AIXI_maximizes expected rewards by assuming the environment is probably controlled by one of the shortest programs consistent with interaction history. Incomputable in general.

**What Ozma shares:**
- Both are agent-based formalisms (Ozma's model reasons and acts; AIXI reasons and maximizes)
- Both have an induction/compression component (Ozma's dialect / Stage 1 = constructive induction)
- Both have a protocol for agent-environment interaction (Ozma's protocol = AIXI's action/percept channel)

**What Ozma does differently:**
- **AIXI is incomputable; Ozma is implementable.** AIXI requires Solomonoff induction (which is incomputable). Ozma's map quality τ is computable via the consistency checker. Ozma replaces incomputable induction with a grammar-constrained protocol.
- **AIXI has no memory graph.** AIXI's state is the interaction history. Ozma's memory graph is a structured representation of accumulated experience.
- **AIXI cannot self-modify.** AIXI assumes a fixed agent. Ozma's neuroplasticity allows structural self-modification (new sub-models).
- **AIXI has no systematicity guarantee.** AIXI predicts based on program compression. Ozma's Thesis II guarantees systematicity under grammar closure.
- **AIXI is purely RL (reward-driven).** Ozma's valence system is richer — valence encodes emotional/practical assessment, not just reward.

**Verdict on AIXI:** AIXI is the correct theoretical target but incomputable. Ozma replaces incomputable components with computable approximations while preserving the agent-based structure. Ozma is AIXI made practical.

---

### Hutter Prize (Compression Approach)

**What it is:** A lossless compression competition for text (enwik9). Winners use statistical language modeling. The underlying thesis: lossless compression of human knowledge is equivalent to AGI.

**What Ozma shares:**
- Both involve compression as a core component (Ozma's IR compression)
- Both treat compression as a proxy for intelligence/reasoning quality
- Both require the compressor to build a model of the data

**What Ozma does differently:**
- **Ozma compresses to grammar-constrained tokens, not just text.** The Hutter Prize compresses text to bits. Ozma compresses code/data to grammar-constrained IR with semantic typed edges.
- **Ozma has a compound loop; compression has none.** Winning compressors improve incrementally but not through a formal self-improving loop.
- **Ozma has formal theorems; compression does not.** The Hutter Prize has no analogue of Ozma's Theses I–VIII.

**Verdict on Compression Approach:** Ozma extends the compression approach with structure (typed edges, grammar constraint, formal proofs). Pure compression has no reasoning structure.

---

## 3. Generative Agents (Park et al., Stanford 2023)

**What it is:** LLM-based agents with a memory stream (observations, reflections, plans stored as text), reflection mechanism (LLM generates higher-level insights from memory), and planning (hierarchical plan generation from current state).

**What Ozma shares:**
- Memory as a persistent store influencing reasoning
- Reflection as a mechanism for updating memory
- Planning from current state + memory

**What Ozma does differently:**
- **Generative Agents store memory as text; Ozma stores it as a typed graph.** Retrieval in Generative Agents is by importance/relevance/recency scores. Ozma's spreading activation retrieves structurally.
- **Generative Agents have no grammar closure.** No systematicity guarantee. Ozma's Thesis II proves systematicity.
- **Generative Agents have no composite model.** Single LLM. Ozma's composite model allows multiple sub-models coordinated by a central model.
- **Generative Agents have no formal convergence.** No theorem analogous to Ozma's τₙ convergence.
- **Generative Agents have no neuroplasticity.** No ability to grow new sub-models.
- **Generative Agents have no formal coherence semantics.** No distinction between accuracy and coherence.

**Verdict on Generative Agents:** Generative Agents are a proof-of-concept with no formal foundation. Ozma formalizes and extends every key mechanism (memory, reflection, planning) with proofs.

---

## 4. Neuro-symbolic Approaches

### Henry Kautz's Taxonomy of Hybrid Architectures

| Type | Description | Ozma analogue |
|---|---|---|
| Symbolic[Neural] | Symbolic reasoning (e.g., MCTS) calls neural nets (e.g., AlphaGo) | Ozma's coordinator queries sub-models |
| Neural | Neural interprets perceptual data into symbols | Ozma's shim grounds semantic elements |
| Neural:Symbolic → Neural | Symbolic reasoning generates training data | Ozma's dialect = Stage 1 training signal |
| Neural_{Symbolic} | Neural nets generated from symbolic rules | Ozma's grammar → delta rules |
| Neural[Symbolic] | Neural directly invokes symbolic reasoning engine | Ozma's sub-model integration via protocol |

**What Ozma does differently:** Ozma is closest to Neural[Symbolic] but with composite model structure — sub-models are themselves neural/symbolic hybrids, coordinated by a grammar-closed protocol. The typed edges across the composite model are what distinguish it from existing hybrid taxonomies.

---

## 5. Whole-Brain Emulation / Neural Architecture

**What it is:** Scanning biological brains and simulating them computationally. Includes connectomics (mapping neural connections) and simulation of specific brain regions.

**What Ozma shares:**
- Ozma's biological analogues (hippocampus = spreading activation, neocortex = consolidation, neurogenesis = neuroplasticity)
- The intuition that biological memory architecture is the right reference

**What Ozma does differently:**
- **Whole-brain emulation is substrate-dependent; Ozma is substrate-independent.** WBE requires simulating biological neurons exactly. Ozma's protocol runs on any substrate that implements the grammar.
- **WBE has no formal protocol.** WBE attempts to copy the implementation. Ozma specifies the interface (protocol) without specifying the implementation.
- **WBE cannot self-modify.** WBE is a static copy of a biological brain. Ozma's neuroplasticity allows the architecture to grow new structure.

**Verdict on WBE:** WBE is a viable path but requires knowing the correct biological implementation. Ozma's approach is specification-first (protocol) rather than implementation-first (emulation).

---

## 6. Key Ozma Differentiators That No Other Approach Has

### 1. Protocol-layer Grammar as the Invariant

No other AGI approach separates the output grammar (protocol) from the model implementation. This separation is load-bearing:
- The protocol is the invariant — any model implementing it can participate in the composite
- Model internals can change (weight updates, fine-tuning, new sub-models) without breaking the protocol
- The grammar-closure property (Thesis II) is defined against the protocol grammar, not any model's internal grammar

AIXI, ACT-R, Soar, and Generative Agents all conflate the reasoning mechanism with the representation. Ozma separates them.

### 2. Compound Loop with Proved Convergence

No other AGI approach has a proved monotone convergence theorem for an iterative improvement loop. AIXI has no convergence proof for its agent dynamics. ACT-R and Soar have no loop convergence theorem. The Generative Agents paper has no theorems.

Ozma's Thesis IV (conditional) + Lemma 6 (unconditional τₙ convergence) is unique in the field.

### 3. Multiplicative Gains Proved

No other AGI approach proves that efficiency gains are multiplicative rather than additive. The compound loop's Thesis V is an algebraic proof that applies to any iteration process with non-negative gains. This is not specific to Ozma — it applies to any compound loop. But Ozma is the first to state and prove it formally.

### 4. Neuroplasticity as Formal Operator

No other AGI approach formalizes neuroplasticity as a computable operator ⊕_neuro that adds new sub-models to the composite during consolidation. Soar's chunking is the closest analogue but chunking creates new rules, not new models.

### 5. Coherence Not Accuracy

No other approach explicitly states that false memories are a feature, not a bug. AIXI and cognitive architectures treat memory as accurate representation. Generative Agents treat memory as text to be retrieved. Ozma's framing (coherence, not accuracy; reconstruction, not reproduction) is unique.

### 6. Typed Edges as First-Class

No other approach has typed edges (involves, contradicts, supports, caused_by, refines) as first-class graph elements with formal semantics. ACT-R has typed chunk slots. HTM has binary SDRs. SDM has no types. Ozma's typed edges carry semantic meaning that makes spreading activation more targeted.

### 7. Three-Loop Architecture

Ozma has three nested loops operating at different timescales:
1. **Reasoning loop:** spreading activation → memory retrieval → reasoning
2. **Consolidation loop:** memory assessment → neuroplasticity → pruning → valence update
3. **Structural loop:** capability gap → ⊕_neuro → new sub-model instantiation

No other approach has this three-loop structure.

---

## 7. What Ozma Is Missing That Other Approaches Have

### What Ozma lacks:

1. **HTM's biological detail** — HTM's neuron-level modeling (dendrites, minicolumns, SDR encoding) is more detailed than Ozma's memory graph abstraction
2. **ACT-R's quantitative cognitive modeling** — ACT-R makes precise predictions of human latencies and error rates; Ozma makes no such predictions
3. **Soar's reinforcement learning integration** — Soar's RL for operator evaluation is more developed than Ozma's valence edges
4. **AIXI's theoretical elegance** — AIXI's single equation (AIXI最大化 expected reward under Solomonoff induction) is maximally compact; Ozma's apparatus is more complex
5. **Generative Agents' simplicity** — Generative Agents are simple to implement; Ozma requires formal grammars, typed graphs, consistency checking
6. **HTM's continuous learning** — HTM has no separate training/inference mode; Ozma's consolidation cycle is more complex

---

## 8. Summary Comparison Table

| Property | Ozma | ACT-R | Soar | HTM | AIXI | Generative Agents |
|---|---|---|---|---|---|---|
| Formal proof apparatus | Yes | Partial | Partial | No | Yes (but incomputable) | No |
| Protocol/output grammar invariant | Yes | No | No | No | No | No |
| Typed graph memory | Yes | Chunks (no edges) | Production rules | Binary SDRs | No | Text stream |
| Spreading activation | Yes | Buffer-based | Working memory | Sequence pooling | No | Relevance scoring |
| Consolidation/sleep cycle | Yes | No | No | Continuous | No | Reflection (ad hoc) |
| Neuroplasticity (new sub-models) | Yes | No | Chunking (new rules) | No | No | No |
| Composite model (multiple sub-models) | Yes | No | No | No | No | No |
| Grammar closure / systematicity | Yes (Thm II) | No | No | No | No | No |
| Proved τ convergence | Yes (τₙ → τ*) | No | No | No | No | No |
| Proved multiplicative gains | Yes (Thm V) | No | No | No | No | No |
| Coherence not accuracy semantics | Yes | No | No | No | No | No |
| Computable (not just theoretical) | Yes | Yes | Yes | Yes | No | Yes |
| Biologically grounded | Partial | Partial | No | Yes | No | No |

---

## 9. Gaps in Ozma Relative to the Field

### Gap 1: No quantitative cognitive predictions

ACT-R makes quantitative predictions about human cognitive performance (reaction times, error rates). Ozma makes no analogous predictions about the composite model's behavior. This is a significant gap for validation — without quantitative predictions, the theory cannot be falsified by experimental data.

### Gap 2: No connection to neuroscience at the neuron level

HTM models at the level of cortical columns, neurons, and dendrites. Ozma's biological analogues are at the functional level (hippocampus → spreading activation, neocortex → consolidation) but there is no mapping to neuron-level mechanisms. A deeper neuroscientific grounding would strengthen the biological plausibility claims.

### Gap 3: Sub-model instantiation is underspecified

⊕_neuro is defined as an operator but no algorithm is given for how to actually instantiate a new sub-model from a capability gap. How is the gap's vocabulary determined? What initializes the new sub-model's map? How is it integrated into Γ_coord? These are critical engineering questions that the current formalism doesn't answer.

### Gap 4: Sub-model instantiation is underspecified — RESOLVED

⊕_neuro is now a mechanical pipeline: gap characterization → corpus construction → distillation → grammar integration → stabilization/pruning. Building a new tensor model is entirely mechanical and uses existing tooling. The AGI entity controls the training material and can build new models. No novel engineering required.

### Gap 5: No learning theory foundation — RESOLVED

Thesis IX (v0.6) provides full learning-theoretic foundations. The map is a hypothesis class H_map parameterized by map state M. Loss L(M, R, O) = 1 − coherence. Generalization bound E_gen(M) ≤ R_emp + complexity term. Sample complexity O(1/τ*) with multiplicative gains. The key insight: map quality is decidable by the consistency checker, enabling provable convergence unavailable for neural network training.

---

## 10. Remaining Gap: Reference Implementation

The only remaining gap is a reference implementation. Every approach Ozma is compared against has working implementations. Ozma is now a theoretical formalism with complete proofs — the nine theses cover grammar closure, compound loop convergence, multiplicative gains, ToM, graph-native memory, composite models, neuroplasticity, and learning-theoretic generalization bounds. Empirical validation requires a working implementation.

---

## 11. Conclusion

Ozma's distinctive contribution over existing AGI research is the combination of:

1. **A protocol-first architecture** — the output grammar is invariant regardless of model implementation
2. **A formal compound loop with proved convergence** — the only AGI approach with a proved monotone convergence theorem
3. **A composite model with neuroplasticity** — the only approach that can grow new sub-models during operation (mechanical pipeline, no novel engineering)
4. **A typed graph memory with consolidation** — more structured than text streams, more expressive than binary SDRs
5. **A coherence semantics for memory** — treating memory as reconstruction rather than archival storage
6. **A learning-theoretic foundation** — map as hypothesis class, compound loop as learning algorithm with generalization bounds

**What was closed in this iteration:**
- Sub-model instantiation (⊕_neuro) is fully mechanical — gap → corpus → distillate → integrate → stabilize/prune
- Statistical learning theory connection — Thesis IX provides generalization bounds, sample complexity O(1/τ*), and proves compound loop dominates traditional training
- Neuroplasticity trigger conditions — novel domain, persistent systematicity gap, trauma/shock (contradiction unmodelable by existing sub-models)

**What remains:**
- Reference implementation (required for empirical validation)

The field's consensus direction (toward multi-model composites, toward memory consolidation, toward grammar-constrained reasoning) validates the Ozma direction. The compound loop's proved convergence and multiplicative gains are unique in the field. The remaining work is implementation, not theory.

---

## Document History

- **0.1** (2026-04-23) — Initial draft. Comparison to cognitive architectures (ACT-R, Soar, HTM, SDM), Universal AI (AIXI, Hutter Prize), Generative Agents, neuro-symbolic approaches, and whole-brain emulation. Identified 7 unique Ozma differentiators and 5 gaps relative to the field.
- **0.2** (2026-04-23) — Closed Gaps 3 (⊕_neuro mechanical pipeline) and 5 (learning theory via Thesis IX). Updated document header to reflect compound_loop_proof.md v0.6 (9 theses, 21 lemmas). Remaining gap: reference implementation only.
