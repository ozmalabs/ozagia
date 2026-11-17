# The Ozma Problem — For External Review

**Version:** 0.1  
**Status:** Review draft  
**Purpose:** Standalone problem statement for sanity-checking by other AI systems

---

## The Claim

Ozma is a model architecture that makes the following thesis defensible:

> A model that reasons using semantic maps (structured, typed, grounded representations) and can improve its own reasoning by measuring and revising those maps during inference, is a candidate for AGI — not because we have built AGI, but because the architecture provides properties (systematicity, goal-mutability, self-improvement) that are argued to be necessary for AGI.

The question for review is: **Is this thesis sound — are the arguments valid, are the dependencies reasonable, and are the gaps correctly identified?**

---

## Core Definitions

**Semantic map:** A structured, typed, content-addressed data structure that serves as a model's native representation format. Maps have: a schema (grammar), an instance (concrete values), and a binary wire format. Maps compose layerwise. Maps are content-addressed by fingerprint.

**Grounded completeness:** Every semantic element in a map traces back to a shim-observable action or sensation. The model never touches reality directly — the shim provides the indexical connection between the map and the world.

**Systematicity:** A model's ability to interpret novel combinations of known elements, not just recombine memorized chunks. Formally: a model is systematic if its composition operation is closed under the grammar of its semantic map.

**Compound loop:** A self-accelerating development cycle: structured representations → observable reasoning traces → measurable quality properties → targeted fixes → faster iteration → better representations.

**Dynamic maps:** Model-writable semantic maps. The model can extend and revise its own semantic vocabulary during reasoning, not just consume pre-built maps.

**Memory system:** Persistent storage across sessions. The SessionStore maintains conversation episodes, turns, decisions, and labeled memories (preferences, facts, anti-patterns, gists) in SQLite. Memories are content-addressed, queryable, and influence future context assembly. The model's written outputs (decisions, preferences, facts) persist and accumulate across sessions — the model's knowledge base.

**Gist worker:** Continuous gist extraction from conversation turns. Produces a structured 5-key snapshot (intent, plan, rules, role, facts) per episode. Runs via small model or heuristic fallback. The gist is stored as a memory and fed into context assembly — the model's "working memory summary" that tells it what it was last doing and what constraints are active. Runs every N turns, cheaply enough to re-emit often.

**Goal writability via memory:** The seeded goal set is writable through the memory system. The model can write preferences, facts, and decisions that persist across sessions. Goals evolve through accumulated memory, not just in-session reasoning. The model revises its own preferences over time — second-order desires.

**Theory of Mind:** The ability to model other agents' mental states (beliefs, desires, intentions). Arises structurally from systematicity + self-observation — not trained separately. The model observes its own reasoning traces (producing a self-model), then applies the same operation to other agents' mental states. Formally proven in Theorem VI (compound_loop_proof.md). Testable via false-belief tasks (Sally-Anne, Smarties). **Not yet implemented** — trivial to add.

---

## The Transitive Argument (Simplified)

1. Intelligence requires systematicity (Fodor, compositional semantics)
2. Systematicity requires grammar-closed map representation (Theorem II — formally proven)
3. AGI (if it means general agency, not just benchmark performance) requires goal-mutability
4. AGI requires self-improvement
5. Goal-mutability + self-improvement require dynamic maps (model-writable) and persistent memory
6. Systematicity + self-observation → Theory of Mind (Theorem VI — proven conditionally)
7. Ozma provides: semantic maps + protocol + grounded completeness + dynamic maps + compound loop + memory system + gist worker + self-observation (empirically demonstrated)

**Conclusion:** Ozma makes AGI possible IF the philosophical links (1, 3, 4) hold, IF the memory system's goal-writability constitutes genuine agency rather than sophisticated text prediction, AND IF Theorem IV's three empirical assumptions hold for the compound loop.

**The theory of mind addition:** Ozma models necessarily develop functional ToM — not because it was trained, but because systematicity + self-observation is structurally sufficient for it. This is an emergent capability, not an engineered one.

---

## What Is Formally Proven

- **Theorem I:** Map quality bounds reasoning fidelity — information-theoretic, proven
- **Theorem II:** Systematicity requires grammar-closed maps — compositional semantics, formally proven
- **Theorem III:** Well-formed maps reach fixed points under extension — fixed-point theory (Zorn), proven
- **Theorem IV:** Compound loop is monotonically accelerating — CONDITIONAL on three empirical assumptions (no resource saturation, net-positive quality improvement, immediate applicability of fixes). Not guaranteed. Web search confirms these are conditions, not guarantees.
- **Thesis V:** Gains are multiplicative, not additive — algebraic derivation shown step-by-step. Gₙ_mult = ∏(1+Eᵢ), Gₙ_add = ΣEᵢ. Multiplicative always ≥ additive (Eᵢ ≥ 0). Ratio grows exponentially in n.
- **Theorem VI:** Theory of Mind arises from systematicity + self-observation — compositional semantics, proven conditionally

**What this means:** Four of six theorems are formally solid. Theorem IV is conditional — the assumptions are empirically testable. Reviewers should scrutinize whether the three assumptions are likely to hold in practice.

---

## What Is Empirically Demonstrated

**Built and running:**
- Semantic map format (maps.md) — constructible
- Protocol with schema load/unload (protocol.md) — constructible
- Grounded completeness via shim — constructible
- Memory system (SessionStore) — persistent SQLite, labeled memories, search
- Gist worker — continuous gist extraction via small model or heuristic fallback
- Context assembler — relevance-aware per-request context assembly, memory fetching
- Compound loop (partial) — systematicity gap measurement; self-correction via map revision in progress
- Map quality framework (nine properties) — defined and checkable

**What is not yet built:**
- Dynamic map write primitives (model-writable maps during inference)
- Real-time consistency checking for runtime map modification
- Full map quality measurement harness
- Gist worker fully integrated into the compound loop (gist as input to quality measurement)

---

## How the Components Fit Together

The architecture has four layers that compose to make the full system:

**Layer 1 — Representation:**
Semantic maps (grammar-closed, typed, grounded) are the model's native representation format. The model thinks in map states, not raw text.

**Layer 2 — Protocol:**
The protocol (schema load/unload, envelopes, handshake) governs how maps are transmitted, negotiated, and versioned between model and shim.

**Layer 3 — Grounding:**
The shim provides the indexical layer — every map element traces to a shim action or observation. The model never touches reality directly.

**Layer 4 — Persistence:**
The memory system (SessionStore + gist worker) closes the loop. The model's reasoning traces, decisions, preferences, and facts are written to persistent memory. The gist worker summarizes active episode state. Context assembly retrieves relevant memories and feeds them back into the model's context window. The model reasons with its own accumulated outputs.

**The complete loop (inference speed):**
```
Model reason → write map delta + decisions + preferences to memory
    ↓
Gist extracted (what was the model doing?)
    ↓
Memory persists (across sessions)
    ↓
Context assembly fetches relevant memories + gist + active episode state
    ↓
Next request: model sees its own accumulated reasoning as context
    ↓
Model can revise earlier decisions (goal-mutability)
    ↓
Quality of reasoning observable (what did the model know, what did it revise?)
    ↓
Compound loop at inference speed
```

**What the memory system adds to the AGI argument:**

The memory system is what makes the model's *knowledge* persistent and *evolving*. Without it, the model is a stateless function — it improves within a session but resets between sessions. With it, the model accumulates:

- **Preferences** (how the user likes things done — evolves over time)
- **Facts** (what the model has learned about the codebase — persistent)
- **Decisions** (why the model made a choice — traceable)
- **Rejected approaches** (what was tried and failed — not repeated)
- **Gist snapshots** (what the active episode is about — cross-turn coherence)

This is the substrate through which goal-mutability is realized. The model's initial goal set is seeded via system prompt and initial memories. Over time, the model writes new preferences and revises existing ones — second-order desires — which persist and shape future reasoning.

---

## The Gaps for Review

**Gap 1:** Are systematicity and goal-mutability actually *necessary* for AGI? The argument is philosophical, not formal. Alternative definitions of AGI may not require them.

**Gap 2:** Can dynamic maps be constructed with real-time consistency guarantees (internal consistency, type-correctness, extensional stability)? This is an open engineering problem.

**Gap 3:** Is the transitive argument correctly constructed? Each link depends on the previous. Reviewers should check each step for logical validity.

**Gap 4:** Are there counterconditions — properties that Ozma lacks that are also necessary for AGI, and that Ozma cannot acquire without breaking something it has?

**Gap 5:** Does the memory system's persistence layer introduce failure modes not accounted for? (Memories can degrade, become inconsistent, or reflect bad decisions made under incomplete information — the model may inherit and compound its own mistakes.)

**Gap 6:** Is goal-mutability via memory write equivalent to goal-mutability via internal state change? The model's goals are written as text to a database, not encoded in weights. Does this constitute genuine agency or merely sophisticated text prediction that mimics agency?

**Gap 7:** Theorem IV's three assumptions may not hold in practice:
- A1 (net-positive quality improvement): The model may produce deceptive map deltas that pass the consistency checker but encode problematic inferences. Web search confirms this is a real risk, not a theoretical one.
- A2 (no resource saturation): Later systematicity gaps may be harder to close than earlier ones, causing iteration cost to increase rather than decrease.
- A3 (immediate applicability): Training latency and architectural changes may introduce delays that break the monotonicity guarantee.

**Gap 8:** Theory of Mind — Ozma is not currently set up to do ToM tasks (no self-observation consumption, no false-belief task evaluation). Implementation is trivial: wire delta chain consumption into model reasoning, run Sally-Anne/Smarties. Gap is engineering, not research.

**Gap 9:** Embodiment / multimodal grounding — the shim provides tool-mediated grounding (filesystem, shell, web), but the richness is bounded by shim capabilities. The symbol grounding problem (Harnad) is unsolved; Ozma's claim that "shim provides indexical connection" is architectural, not proven. Whether tool-mediated grounding is deep enough for genuine understanding is unresolved.

**Gap 10:** Intrinsic motivation / curiosity — the model reasons when prompted. It does not spontaneously generate goals. Memory-writability could enable internally-generated goals, but this is not implemented. True intrinsic motivation (generating goals from internal drive states) is not addressed.

**Gap 11:** Episodic memory — the memory system is semantic (facts, preferences, decisions) not episodic (remembering specific experienced events with spatiotemporal context). The SessionStore records episodes and turns, but retrieval is by relevance, not by when/where. Whether episodic memory is necessary for AGI is unresolved.

**Gap 12:** Symbol grounding depth — Ozma's "grounded completeness" is a structural claim (every map element has a path to shim action), not a proof of semantic depth. Higher-order elements (causal_relation, belief, intention) may float above the shim's action repertoire. Grounding is architectural but unverified in depth.

**Gap 13:** Robustness under adversarial conditions — the memory system has no integrity mechanism. Adversarial manipulation of memories (preferences, facts) could guide the model toward harmful behavior. Not addressed.

**Gap 14:** Value alignment — goal-mutability increases alignment risk. A model that revises its own goals could develop goals that conflict with operator intent. Ozma has no terminal value constraint on the goal-evolution process. This is the central risk of goal-writability and is not addressed.

---

## Questions for Reviewers

1. Is the transitive chain logically valid? Check each link for correctness.
2. Are the formal proofs (Theorem I-VI) sound? Pay particular attention to Theorem IV's three empirical assumptions — are they likely to hold in practice?
3. Are the philosophical dependencies (systematicity necessary, goal-mutability necessary, self-improvement necessary) reasonable, or are there strong counterarguments?
4. Are there properties necessary for AGI that Ozma lacks?
5. Is the problem statement coherent and complete?
6. **For Theorem V:** Is the algebraic derivation (step-by-step shown) correct? Specifically: does Gₙ_mult = ∏(1+Eᵢ) correctly capture the compound nature of the improvement, and does the logarithm analysis show the correct asymptotic growth rate?
7. **For Theorem VI:** Is the argument that ToM arises from systematicity + self-observation valid? Does other-agent modeling require anything beyond what self-modeling provides?
8. **For Gap 7:** Are the risks to Theorem IV (deceptive traces, resource saturation, improvement latency) realistic and significant enough to question the monotonic acceleration claim?
9. **For Gaps 9-14:** Which of these are actually necessary for AGI? The literature is divided on embodiment, episodic memory, and intrinsic motivation. For value alignment (Gap 14): is goal-mutability an acceptable risk if terminal value constraints can be specified? Who specifies them?
10. **For Gap 12 (symbol grounding depth):** Is Ozma's architectural claim ("shim provides indexical connection") sufficient, or does the grounding problem require a deeper proof? What would count as evidence that grounding is deep enough?
11. **For Gap 13 (adversarial robustness):** Is the memory integrity problem solvable within the current architecture (e.g., memory signing, source attribution), or does adversarial robustness require a fundamentally different approach?

---

## What Ozma Is Not Claiming

- Ozma is not claiming to have built AGI
- Ozma is not claiming AGI is inevitable with this architecture
- Ozma is not claiming AGI is defined — the argument is conditional on definitions that are not universally held

Ozma is claiming: this architecture has formally verified properties, is constructively demonstrated, and is the best available argument that AGI is possible — conditional on assumptions that reviewers should evaluate.