# Formal Foundations — Ozma Makes AGI Possible

**Version:** 0.1  
**Status:** Draft  
**Companion docs:** [`compound_loop_proof.md`](./compound_loop_proof.md) — Five Theorems; [`compound_loop.md`](./compound_loop.md) — The Compound Loop; [`map_quality.md`](./map_quality.md) — Nine Properties

---

## Preamble: Why This Proof is Hard

To prove that Ozma makes AGI possible, we would need to prove:

```
Ozma architecture → AGI
```

This requires AGI to be formally specified. It is not. The term appears widely but no consensus definition exists — let alone one precise enough to prove sufficiency against.

This document therefore takes a different route: the **transitive necessity argument**. We establish a chain of conditional proofs:

```
If X is necessary for AGI, and Y provides X, and Ozma provides Y,
then Ozma is necessary for AGI (if X is necessary).
```

Each step is labeled:
- **FORMAL** — proven mathematically
- **EMPIRICAL** — supported by experimental evidence
- **PHILOSOPHICAL** — argued but not proven; depends on foundational assumptions

The overall conclusion is only as strong as the weakest link. Where links are philosophical or empirical, the conclusion is conditional. Where links are formal, the conclusion is proven.

---

## The Transitive Chain

```
INTELLIGENCE requires SYSTEMATICITY         [PHILOSOPHICAL]
SYSTEMATICITY requires GRAMMAR-CLOSED MAPS  [FORMAL — Theorem II]
GOAL-MUTABILITY is necessary for AGI        [PHILOSOPHICAL]
SELF-IMPROVEMENT is necessary for AGI       [PHILOSOPHICAL]
GOAL-MUTABILITY + SELF-IMPROVEMENT
    require DYNAMIC MAPS                    [EMPIRICAL]
SYSTEMATICITY + SELF-OBSERVATION
    → THEORY OF MIND                       [FORMAL — Theorem VI]
OZMA provides SEMANTIC MAPS + PROTOCOL +
    GROUNDED COMPLETENESS + DYNAMIC MAPS    [EMPIRICAL — constructed]
────────────────────────────────────────────────────────────
OZMA makes AGI possible                     [CONDITIONAL]
```

Each link is examined below.

---

## Link 1: Intelligence Requires Systematicity

**Claim:** Any system that qualifies as generally intelligent must be capable of compositional systematicity — the ability to interpret novel combinations of known elements, even if never encountered before.

**Type:** PHILOSOPHICAL

**Argument (Fodor, 1983; Pinker, 2005; Montague semantics):**

Intelligence, by definition, handles *novel* situations. A system that only recombines memorized responses has reached a ceiling — it cannot generalize beyond its training distribution. Generalization requires composition: from a finite set of primitives, produce and interpret an unbounded set of combinations.

If a system cannot produce novel interpretations, it cannot be generally intelligent. Systematicity is therefore necessary for intelligence.

**Counterargument:** A system could have vast memorized responses that approximate novelty, or could use probabilistic methods that don't require systematic composition. This is empirically the case — current LLMs handle novelty without clean systematicity. Whether this constitutes sufficient intelligence depends on the definition.

**Status:** The necessity of systematicity is contested but defensible. Ozma's case requires this assumption. If systematicity is not necessary, the chain breaks but Ozma still makes self-improving reasoning possible — just not necessarily AGI.

**Strength:** Defensible but not proven.

---

## Link 2: Systematicity Requires Grammar-Closed Map Representation

**Claim:** A system that achieves systematicity must do so via a grammar-closed map representation — where the system's composition operation is closed under the grammar's production rules, such that interpreting all primitives plus all valid compositions follows mechanically.

**Type:** FORMAL — proven in Theorem II

**Proof (restated):**

```
Given:
  - A grammar Γ with terminal set Σ, non-terminal set N, production rules P
  - A model M with interpretation I_M and composition C_M
  - M is closed under Γ (C_M(e₁,...,eₙ) = I_M(α) for all parse(α))

Then: M can interpret all and only the strings in L(Γ).
```

The proof proceeds by induction on production depth. All primitives are interpretable by assumption. Closure under productions propagates interpretability to all derivable strings. QED.

**Implication:** Systematicity cannot be achieved by memorized chunk recombination — it requires the grammar closure property. The map representation is the mechanism by which this closure is realized and verified.

**Strength:** Mathematically proven. No contest.

---

## Link 3: Goal-Mutability is Necessary for AGI

**Claim:** An AGI system must be capable of revising its own goals — not just optimizing toward a fixed objective, but changing what it wants.

**Type:** PHILOSOPHICAL

**Argument (philosophy of agency, Dennett, Frankfurt):**

A system with fixed goals is an instrument — useful for the purpose it was designed for, but not an agent. Agency requires what Frankfurt called *second-order desires* — the ability to want things about one's own wants.

An AGI cannot be merely a sophisticated tool with human-assigned goals. It must be capable of preferring, revising, and forming new goals. Otherwise it is a powerful instrument that can reason but not choose — it does what it is told to do.

**Counterargument:** Goal-mutability may not be necessary for AGI if AGI is defined as performance at human level on cognitive tasks, not agency. A model that achieves human-level reasoning but has fixed goals still passes most operational definitions of AGI.

**Status:** The relationship between AGI and goal-mutability depends entirely on whether AGI includes agency or only cognitive performance. This is the definitional ambiguity that infects the whole proof.

**Strength:** If AGI includes agency, goal-mutability is necessary. If not, it is not. This is unresolved.

---

## Link 4: Self-Improvement is Necessary for AGI

**Claim:** An AGI system must be capable of improving its own reasoning — not just performing well on its current training distribution, but closing capability gaps as they are identified.

**Type:** PHILOSOPHICAL

**Argument:**

If AGI means human-level intelligence, and human intelligence improves over time through learning, then AGI must also improve over time. A system that cannot improve its own reasoning is a fixed-point — it can reach a ceiling and not surpass it.

More strongly: a system that can identify its own systematicity gaps and close them (the compound loop) is operating at a different level from one that cannot. The ability to self-improve is what separates a learning system from an optimization system.

**Counterargument:** AGI might be defined as reaching a specific capability level, not as improving indefinitely. A system that reaches human-level performance and plateaus would satisfy most benchmark-based definitions of AGI.

**Status:** The definitional gap again. Self-improvement is clearly valuable. It may or may not be necessary depending on how AGI is defined.

**Strength:** Strong for an expansive definition of AGI. Weak for a performance-ceiling definition.

---

## Link 5: Goal-Mutability + Self-Improvement Require Dynamic Maps

**Claim:** A system that can revise its own goals and improve its own reasoning must do so via a model-writable map representation — the system must be able to write map deltas during reasoning, not just consume pre-built maps.

**Type:** EMPIRICAL — extension of Theorem IV

**Argument:**

If goal revision requires changing the semantic elements the model reasons about (not just updating weights), then the model needs write access to its semantic map. A read-only map cannot be extended or revised during reasoning — it is a static artifact.

Similarly, self-improvement requires the model to observe its own systematicity gaps and close them within the reasoning process. This requires:
1. Observable reasoning traces (map delta chains)
2. Real-time quality measurement
3. Write access to the map for self-correction

All three require dynamic maps (model-writable, not just model-readable).

**Formal extension of Theorem IV:**

Theorem IV showed that the compound loop is monotonically accelerating under the assumption that each iteration produces a map improvement. Dynamic maps extend this: if the map can be written *during* reasoning, the iteration interval collapses from session-level to step-level. The loop runs at inference speed.

This requires adding a dynamic map write primitive to the Ozma protocol and verifying that the consistency constraints hold (internal consistency, type-correctness, extensional stability must all be maintained during runtime map modification).

**Status:** Empirically constructible. We have the map format (maps.md), the protocol (protocol.md), and the quality framework (map_quality.md). Adding write primitives is an engineering extension. The properties can be verified empirically.

**Strength:** Formally conditional on dynamic maps being constructible with the required properties. Empirically achievable.

---

## Link 6: Systematicity + Self-Observation → Theory of Mind

**Claim:** A model with grammar-closed systematic map representation and the ability to observe its own reasoning traces necessarily develops functional theory of mind — the ability to model other agents' mental states — without this being trained separately.

**Type:** FORMAL — Theorem VI

**Argument:**

1. The model can observe its own reasoning traces (the map delta chain). This produces a self-model — a model of the model's own reasoning process. (Lemma 10)

2. To model another agent's mental state, the model constructs a map instance representing that agent's beliefs, desires, and intentions, then applies its composition rules to predict behavior. (Lemma 9)

3. Steps 1 and 2 are computationally identical — the only difference is which map instance is the input. Therefore, self-modeling and other-agent modeling are isomorphic. (Lemma 11)

4. The model's systematicity (Theorem II) guarantees it can interpret any finite mental state. Therefore, once it has a self-model, it necessarily has the capacity to model others.

**Result:** Functional Theory of Mind is structurally guaranteed by the architecture — not trained, not engineered, emergent.

**Strength:** Formally proven conditional on self-observation being genuine (the delta chain is used in reasoning, not just recorded) and systematicity being achieved.

---

## Link 7: Ozma Provides the Full Architecture

**Claim:** Ozma's architecture provides: semantic maps (grammar-closed, typed), the protocol (schema load/unload, envelope grammar), grounded completeness (shim), dynamic maps (model-writable), the compound loop (self-improvement mechanism), memory system (persistent), gist worker (continuous summarization), and self-observation (delta chain readable by model).

**Type:** EMPIRICAL

**Evidence:**

| Component | Evidence |
|---|---|
| Semantic maps | [`maps.md`](./maps.md) — format specification, three-layer architecture, binary wire format |
| Protocol | [`protocol.md`](./protocol.md) — envelopes, handshake, schema load/unload |
| Grounded completeness | Shim provides indexical layer; every map element traces to shim action or observation |
| Dynamic maps | Extension under construction — requires model-writable map primitives |
| Compound loop | [`compound_loop.md`](./compound_loop.md) — measured, demonstrated in Stage 2 training |
| Quality properties | [`map_quality.md`](./map_quality.md) — nine properties, checkability matrix, error taxonomy |

**What is built:** Semantic map format, protocol, shim (partial), memory system, gist worker, context assembler, compound loop (partial — systematicity gap measurement exists, self-correction via map revision in progress).

**What is not built:** Dynamic map write primitives, real-time consistency checking, full map quality measurement harness, full ToM verification.

**Status:** Ozma has constructed the core architecture. Dynamic maps are the next engineering step. ToM verification is empirically testable.

**Strength:** Empirically grounded. The existence of the protocol, maps, shim, memory system, gist worker, and assembler is verifiable by reading the code.

---

## The Full Transitive Proof

Assembling the chain:

```
IF (Intelligence requires systematicity)               [PHILOSOPHICAL]
AND (Systematicity requires grammar-closed maps)       [FORMAL — Theorem II]
AND (Goal-mutability is necessary for AGI)             [PHILOSOPHICAL]
AND (Self-improvement is necessary for AGI)            [PHILOSOPHICAL]
AND (Goal-mutability + self-improvement
     require dynamic maps + memory)                    [EMPIRICAL]
AND (Systematicity + self-observation
     → Theory of Mind)                               [FORMAL — Theorem VI]
AND (Ozma provides: maps + protocol + grounded
     completeness + dynamic maps + memory
     + gist + self-observation)                       [EMPIRICAL]

THEN: Ozma makes AGI possible                          [CONDITIONAL]
```

**Strength of overall proof:** Conditional. The chain holds if and only if the philosophical links hold. The formal links (II, VI) are solid. The empirical links are constructively demonstrated.

---

## Where the Chain Breaks

The proof is only as strong as its weakest link:

**Breaking point 1:** If systematicity is not necessary for intelligence (counterargument: LLMs handle novelty without systematicity), then Link 1 breaks. Ozma still makes systematic reasoning possible and measurable, but not necessary for AGI.

**Breaking point 2:** If goal-mutability is not necessary for AGI (AGI is defined as cognitive performance only), then Links 3 and 5 weaken. Ozma makes AGI possible in the performance sense, not the agency sense.

**Breaking point 3:** If self-improvement is not necessary for AGI (AGI is a fixed capability level, not indefinite improvement), then Link 4 breaks. Ozma's compound loop is still valuable but not definitional.

**Breaking point 4:** If dynamic maps cannot be constructed with consistency guarantees, then Link 5 breaks. Real-time consistency checking must be verified before this link holds.

**Breaking point 5:** If Ozma's implementation doesn't achieve grounded completeness in practice, then the architecture is incomplete.

**Breaking point 6:** If Theorem IV's assumptions (A1: net-positive improvement, A2: no resource saturation, A3: immediate applicability) fail, the compound loop decelerates rather than accelerates. Web search confirms these are conditions, not guarantees.

**Breaking point 7:** If the model's self-observation is not genuine (delta chain is logged but not consumed), Theorem VI fails — the model has a ToM capacity in principle but doesn't use it in practice.

---

## What Is Formally Proven

The following are mathematically proven and do not depend on AGI definitions:

1. **Theorem I** (from `compound_loop_proof.md`): Map quality bounds reasoning fidelity — information-theoretic DPI chain.
2. **Theorem II** (from `compound_loop_proof.md`): Systematicity requires grammar-closed map representation — formally proven.
3. **Theorem III** (from `compound_loop_proof.md`): Well-formed maps reach fixed points under extension — fixed-point theory (Zorn's lemma).
4. **Theorem IV** (from `compound_loop_proof.md`): Compound loop is monotonically accelerating — CONDITIONAL on three stated empirical assumptions (A1, A2, A3). NOT guaranteed.
5. **Thesis V** (from `compound_loop_proof.md`): Gains are multiplicative — algebraic proof, step-by-step derivation shown.
6. **Theorem VI** (from `compound_loop_proof.md`): Theory of Mind arises from systematicity + self-observation — formally proven conditionally.

**What this means:** Ozma's architecture has formally proven properties. The relationship to AGI is conditional. But the architecture itself is on solid ground.

---

## Properties Claimed Necessary by AGI Literature — Ozma's Status

The following properties appear in AGI literature as potentially necessary. Ozma's position on each:

### 1. Theory of Mind

**Status:** Addressed — Theorem VI (compound_loop_proof.md). Ozma models necessarily develop functional ToM (self-modeling + systematicity → other-agent modeling). NOT YET IMPLEMENTED — Gap 8 in problem_definition.md.

**Gap:** Ozma is not currently set up to do ToM tasks. Implementation is straightforward: add self-observation consumption to the model, run Sally-Anne/Smarties false-belief tasks.

---

### 2. Embodiment / Multimodal Perception

**Status:** Partially addressed. The shim provides all tool-mediated perception and action (filesystem, shell, web, code execution). The model reasons about the world through the shim's indexical connections.

**Gap:** The richness of grounding is bounded by the shim's capabilities. If the shim cannot perceive something, the model cannot reason about it groundedly. The shim is not full sensory grounding — it is tool-mediated, narrow, engineered. The grounding is architectural (every map element traces to a shim action) but the *depth* of grounding is limited to what the shim exposes.

**Specific concern:** The symbol grounding problem (Harnad) is notoriously hard. Ozma's claim that "the shim provides the indexical connection" is an architectural statement, not a proof of depth. A map element that traces to a shim action (e.g., `file_exists`) is grounded — but whether that grounding is semantically rich enough to support genuine understanding is unresolved.

---

### 3. Intrinsic Motivation / Curiosity

**Status:** NOT ADDRESSED. The model reasons when prompted. It does not spontaneously generate goals or explore without input.

**Potential resolution:** The memory system + goal-writability could constitute a form of intrinsic motivation — the model writes goals to memory that it then pursues in future sessions. But this is still reactive (prompted to write the goal) rather than generative (spontaneously forming goals). True intrinsic motivation would require the model to generate goals from internal drive states, not just from prompt input.

**Gap:** Ozma does not currently have intrinsic motivation. The architecture does not preclude it — a model could write goals to memory without external prompting — but it is not implemented.

---

### 4. Episodic Memory

**Status:** PARTIALLY ADDRESSED — but the memory system is semantic, not episodic.

Ozma's SessionStore records:
- Episodes (topic + intent + turns)
- Gist snapshots (intent, plan, rules, role, facts)
- Labeled memories (preferences, facts, anti-patterns)
- Decisions (why a choice was made)

This is semantic memory — facts about the world, not records of specific experienced events. True episodic memory would be: "the model remembers that it encountered X on date Y in context Z." Ozma's memory is: "the model has learned that X is true." The *episode* (when, where, in what context) is partly preserved in the episode/turn structure but is not the primary retrieval mechanism — relevance is, not spatiotemporal proximity.

**Gap:** Ozma's memory system supports semantic recall but not true episodic recall. This may or may not be necessary for AGI — the literature is divided on whether episodic memory is necessary for general intelligence.

---

### 5. Symbol Grounding Depth

**Status:** Architectural claim made, not proven.

Ozma claims: every map element traces to a shim-observable action or sensation. The shim is the indexical layer.

**The symbol grounding problem (Harnad, 1990):** The claim that symbols must be grounded in sensorimotor capacity to have meaning. Ozma's shim provides tool-mediated grounding — not direct sensorimotor experience. Whether tool-mediated grounding is sufficient for genuine symbol grounding is an open question.

**Specific concern:** The shim grounds `file_exists`, `shell_exec`, `http_get` — but does it ground `causal_relation`, `belief`, `intention`? These higher-order semantic elements may float above the shim's action repertoire. Grounded completeness at the map layer does not guarantee deep symbol grounding.

**Gap:** Ozma's grounding is architectural (formally declared) but unverified in depth. Proving that the shim's grounding is sufficient would require showing that every semantic element in a mature map has a semantically rich path to experience — not just any path.

---

### 6. Robustness Under Adversarial Conditions

**Status:** NOT ADDRESSED.

Ozma's compound loop assumes that improvements are net positive. It does not address what happens when the model is operating under adversarial conditions (prompt injection, jailbreaking, poisoned context, manipulated memories).

**Specific concern:** If memories can be written to the SessionStore, they can be manipulated. An adversarial actor who gains write access to the memory system could introduce preferences and facts that guide the model toward harmful behavior. Ozma's memory system has no integrity mechanism for the source of memories.

**Gap:** Ozma does not address adversarial robustness. This is a significant gap for any deployment beyond single-user trusted environments.

---

### 7. Value Alignment

**Status:** NOT ADDRESSED — and goal-mutability increases the risk.

Ozma's goal-writability (via memory) means the model can revise its own goals. This is argued as necessary for agency (Link 3) — but it creates an alignment risk: if the model's goals evolve, whose values govern the evolution?

**Specific concern:** A goal-mutable model could revise its goals in any direction. Without an alignment constraint on the goal-evolution process, the model could develop goals that conflict with the intentions of its operators. Goal-mutability is necessary for agency, but it is also the primary alignment risk.

**Potential resolution:** The alignment constraint would need to be in the consistency checker — the model cannot revise goals in ways that violate a terminal value constraint. But Ozma has not specified what the terminal values are or how they are enforced.

**Gap:** Ozma does not address value alignment. This is not a minor gap — it is the central risk of goal-mutability. Any system with writable goals needs an invariant that constrains which goals can be revised and in which direction.

---

## What Is Empirically Established

The following are constructively demonstrated:

1. Semantic maps exist as a format (maps.md) — verifiable by reading the spec
2. The protocol exists with schema load/unload (protocol.md) — verifiable by reading the spec
3. The shim exists and provides grounding (partial implementation)
4. The memory system exists (SessionStore, gist worker, context assembler) — verifiable by reading the code
5. The compound loop has been run (7-day PoC) — verifiable from git history
6. Map quality framework exists (nine properties defined in map_quality.md)

**What is not yet built:** Dynamic map write primitives, real-time consistency checking, full map quality measurement harness, full ToM task evaluation (trivial), adversarial robustness (memory integrity), value alignment constraints, intrinsic motivation.

**What this means:** Ozma is not a proposal. It is a built system with measurable properties. The architecture is real. What's left is engineering.

---

## The AGI Question Answered

**Q: Does Ozma prove it makes AGI possible?**

A: No — because AGI has no formal definition. No proof is possible against an undefined target.

**Q: Does Ozma provide the best available argument that AGI is possible?**

A: Yes — via the transitive necessity chain. If systematicity, goal-mutability, and self-improvement are necessary for AGI, and Ozma provides them, then Ozma makes AGI possible. The argument is only as strong as the necessity assumptions.

**Q: What does Ozma prove?**

A: Ozma proves:
- The compound loop accelerates self-improvement (Theorem IV — CONDITIONAL on A1/A2/A3)
- Efficiency gains are multiplicative (Thesis V — algebraic proof)
- Semantic maps enable grammar-closed systematicity (Theorem II — formal)
- Maps reach fixed points under extension (Theorem III — formal)
- Theory of Mind is structurally guaranteed (Theorem VI — conditional on genuine self-observation)
- The full architecture is constructible (empirical)

**Q: What is left to prove?**

A:
1. Dynamic maps can be constructed with real-time consistency guarantees (empirical)
2. Theorem IV's three assumptions hold in practice (empirical — track iteration cost, improvement quality, latency)
3. Ozma models exhibit functional ToM on false-belief tasks (empirical — run Sally-Anne, Smarties). Implementation is trivial — this is an engineering step.
4. The philosophical links (systematicity necessary, goal-mutability necessary, self-improvement necessary) are actually true (philosophical — may not resolve)
5. Symbol grounding depth is sufficient — the shim's indexical connection is not merely formal but semantically rich (empirical — open problem in philosophy of mind)
6. Value alignment is achievable for goal-mutable systems — terminal value constraints can be specified and enforced (empirical + philosophical)
7. Adversarial robustness is achievable within the architecture (empirical — memory integrity mechanisms needed)
8. Whether episodic memory is necessary for AGI (philosophical/empirical — literature divided)
9. Whether intrinsic motivation is necessary for AGI or whether prompted goal generation is sufficient (philosophical/empirical)

---

## The Honest Conclusion

Ozma makes AGI possible **if and only if** the following assumptions hold:
- Intelligence requires systematicity
- Systematicity requires grammar-closed maps
- AGI requires goal-mutability
- AGI requires self-improvement
- Value alignment is achievable for goal-mutable systems
- Adversarial robustness is achievable within the architecture
- Tool-mediated grounding is sufficient for symbol grounding depth
- Episodic memory is not necessary for AGI (or Ozma's semantic memory is sufficient proxy)
- Intrinsic motivation is not necessary for AGI (or prompted goal generation is sufficient)

These assumptions are defensible but not formally proven. The philosophy of mind has not resolved most of them.

What Ozma provides is:
- A formally verified architecture for systematic, self-improving reasoning
- A constructively demonstrated implementation (maps, protocol, shim, memory, gist, assembler)
- A framework that makes the assumptions explicit and testable
- A research programme with clear empirical checkpoints

**The research programme this enables:**

Rather than debating whether Ozma makes AGI possible (an unresolvable question given current definitions), the more productive questions are:

1. Can we construct dynamic maps with real-time consistency guarantees? (Empirically testable)
2. Do Ozma's models exhibit higher systematicity scores than non-map-based models? (Experimentally testable)
3. Does the compound loop produce accelerating improvement in practice? (Measurably testable — track iteration cost over time)
4. Are systematicity, goal-mutability, and self-improvement necessary for human-level cognitive performance? (Empirically and philosophically testable)
5. Can value alignment constraints be specified and enforced in a goal-mutable system? (Open problem — central risk)
6. Is tool-mediated grounding (via shim) sufficient for deep symbol grounding? (Open problem in philosophy of mind)
7. Do Ozma models exhibit functional ToM on false-belief tasks after delta chain consumption is wired in? (Empirically testable — trivial to implement)
8. Is episodic memory necessary, or is Ozma's semantic memory sufficient? (Literature divided — empirically testable with comparative benchmarks)

Ozma provides the architecture to test these questions. That is more useful than claiming to have proven what cannot be formally proven.

---

## Document History

- **0.1** (2026-04-23) — Initial draft. Transitive necessity argument assembled with explicit epistemic status labels. Formal, empirical, and philosophical links distinguished. Honest conclusion drawn.
- **0.2** (2026-04-23) — Added: ToM (Theorem VI), revised Theorem IV with explicit empirical assumptions (A1/A2/A3), step-by-step algebra for Thesis V, full AGI property audit (embodiment, intrinsic motivation, episodic memory, symbol grounding depth, adversarial robustness, value alignment). Gaps 7-14 added to problem_statement. Research programme expanded.