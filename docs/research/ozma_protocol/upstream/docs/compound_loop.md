# The Compound Loop — Ozma's Self-Accelerating Architecture

**Version:** 0.1  
**Status:** Draft  
**Companion docs:** [`map_quality.md`](./map_quality.md) — Map Quality Criteria; [`protocol.md`](./protocol.md) — Model Language; [`maps.md`](./maps.md) — Map Format

---

## The problem with current LLM development

Modern LLM development is a blind iteration loop:

```
Train → Evaluate → Fail → Retrain → Repeat
```

Each cycle is expensive (months, millions of dollars), slow (the next training run is the only signal), and opaque (you don't know *why* the model failed, only that it did). Benchmark improvements tell you the model got better at something — they don't tell you what the model now understands that it didn't before, or where its reasoning still breaks down.

The result is a development process where:
- Iteration is measured in *training runs*, not *days*
- Capability gaps are inferred, not observed
- Improvements to one capability may regress another without detection
- Model comparison is output-level only — you can't see *how* a better model reasons differently, only that it produces better outputs

Ozma's compound loop changes this by making reasoning **explicit, measurable, and improvable** — not as an add-on, but as the native output format of the model.

---

## The compound loop

The compound loop is a self-accelerating development cycle built from three properties that Ozma makes possible:

1. **Structured representation** — models reason in semantic maps, not just output text
2. **Observable reasoning** — the delta chain from input to output is followable
3. **Measurable quality** — the nine properties give reasoning a score, not just a label

Together, these enable a development cycle where each iteration is faster, more targeted, and compounds on prior work:

```
BETTER MAPS
     ↓
OBSERVABLE REASONING TRACES
     ↓
MEASURED QUALITY PROPERTIES
     ↓
TARGETED FIXES (not scatter-shot retraining)
     ↓
FASTER ITERATION
     ↓
MORE IMPROVEMENTS PER UNIT TIME
     ↓
BETTER MAPS
     ↑______________|
```

This is not an additive improvement to the development process. It is **multiplicative** — each layer amplifies the effectiveness of the layer above, and the loop accelerates itself.

---

## How the loop works in detail

### Step 1: A model produces a reasoning trace

When a model processes a task using Ozma's protocol, its reasoning is expressed as a sequence of map states and deltas:

```
Input map → [delta₁] → [delta₂] → ... → [deltaₙ] → Output map
```

Each delta is a map operation: adding an entry, updating a confidence value, retiring a hypothesis, composing a sub-map. The sequence encodes the model's reasoning process — every revision, every confidence shift, every grounded inference.

**In a traditional LLM:** this reasoning is invisible. You get the final output. If the reasoning is wrong, you can only infer where it diverged.

**In Ozma:** the trace is the output. The final answer is just the last map state. The reasoning chain is readable.

---

### Step 2: Map quality is measured

Given a reasoning trace, you can measure it against the nine properties:

| Property | What it reveals in a trace |
|---|---|
| Internal consistency | Did the model contradict itself at any point? |
| Compositional systematicity | Does the model apply the same rule consistently across contexts? |
| Grounded completeness | Are all reasoning elements traceable to shim-observable actions? |
| Type-correctness | Is every composition well-typed, or are there category errors? |
| Semantic distinctness | Is the model using tokens consistently, or conflating distinct meanings? |
| Round-trip consistency | Does the model's encoding/decoding stay stable across the trace? |
| Extensional stability | When new information arrives, does reasoning break or adapt? |
| Information efficiency | Is the model carrying redundant entries, or using minimal representation? |
| Productivity | Is the model composing new solutions, or recombining memorized chunks? |

Each property produces a **score**, not just a pass/fail. A model's systematicity might be 0.73 on a task — good relative to another model, but with a specific gap at the composition boundary between `function_definition` and `import_statement`.

**The score is interpretable.** When systematicity is 0.73, you know exactly what the model gets right (intra-context composition) and what it gets wrong (cross-context composition). When benchmark accuracy is 73%, you know only that the model solved 73% of problems.

---

### Step 3: Targeted fixes replace blind retraining

Traditional retraining is the hammer: you change the training data or objective, run the model through another expensive cycle, and hope the capability improves without regressing others.

With measured quality properties, the fix is targeted:

```
Systematicity gap at cross-context composition
     ↓
Identify which semantic elements are involved
     ↓
Check whether the map grammar covers these compositions
     ↓
If map gap → update the map schema or build a more comprehensive map
     ↓
If model gap → augment training corpus with cross-context composition examples
     ↓
No full retraining unless map and corpus both needed
```

The fix is proportional to the gap. You don't retrain the entire model — you close the specific gap the quality measurement revealed.

This is the core of the compound loop's acceleration: **each iteration is cheap and targeted**, so you run more iterations per unit time, and each iteration compounds on the last.

---

### Step 4: Map improvements self-improve the next generation

A mature Ozma build process uses the previous generation's runtime model to quality-check the next generation's map:

```
Generation N runtime model
     ↓ (quality-check)
Generation N+1 semantic map
     ↓ (Stage 2 training)
Generation N+1 runtime model
     ↓ (quality-check)
Generation N+2 semantic map
     ↑_______________|
```

The runtime model is both the *user* of the map and the *auditor* of the next generation's map. It has the most direct experience with how the map performs at inference time — which elements are hard to ground, which compositions break systematicity, which reasoning traces are easy to follow and which are opaque.

This creates a self-improving loop where the map and the model co-evolve: better maps produce better reasoning, which produces sharper quality measurements, which produces better maps.

---

## Why the gains are multiplicative

Consider the contrast:

**Traditional development (additive gains):**

```
Better data + Better architecture + Better training = Better model
     ↑____________________| ← each input is independent
```

Each input improves the output by a fixed amount. Doubling the data budget roughly doubles the improvement. The returns are diminishing but predictable.

**Ozma compound loop (multiplicative gains):**

```
Better maps → Better traces
Better traces → Sharper measurements
Sharper measurements → Targeted fixes
Targeted fixes → Faster iteration
Faster iteration → More improvements per unit time
More improvements → Better maps
     ↑______________|
```

Each step makes the next step more effective. Sharper measurements don't just help you see more — they help you *fix more per iteration*. Faster iteration doesn't just run more cycles — it runs cycles that compound on each other's gains rather than starting fresh each time.

The multiplicative structure means the loop gets faster *as it runs*, not just linear progress but accelerating progress. This is what separates Ozma's development efficiency from additive improvements — the compound interest, not just the individual components.

---

## The three-layer compound effect

The multiplicative gains operate across three layers simultaneously:

### Layer 1: Map quality

A better map (more consistent, more systematic, more grounded) produces better reasoning traces. The model doesn't just perform better — its reasoning is more legible, making the next improvement cycle easier.

**Multiplicative link:** Better maps → traces that are easier to audit → sharper quality measurements → more targeted improvements → better maps.

### Layer 2: Observation fidelity

A better trace (more complete delta recording, more precise entry metadata) gives you more signal per reasoning step. You're not just seeing the reasoning — you're seeing *exactly* where it broke down.

**Multiplicative link:** Higher-fidelity traces → smaller quality gaps are detectable → earlier intervention → less regression accumulated → sharper measurements → better traces.

### Layer 3: Development velocity

Cheaper, more targeted iterations mean more cycles per unit time. More cycles means the loop compounds faster. Faster compounding means the gap between Ozma and traditional development widens over time.

**Multiplicative link:** Faster iteration → more cycles per month → more compounding → gap widens → more resources can be allocated to hard problems → even faster iteration.

---

## The empirical basis

The 3-100x efficiency gains documented in the Ozma project are output-level measurements — cost per task, tokens per solution. These are real and reproducible.

The compound loop is the **mechanism** that explains why those gains are achievable and why they scale in Ozma's favor over time. The 7-day timeline existence proof (protocol + 2 trained LoRAs + shim-on-frontier) was the first iteration of the loop. The compound loop is what makes iteration 2, 3, and N faster and cheaper than iteration 1.

The efficiency gains are the output. The compound loop is the engine.

---

## What this means for Paper 1

The current Paper 1 framing leads with "Ozma was made and proves its own thesis" as the core tenet — a self-referential existence proof that Ozma demonstrates what's possible with component-based model architecture.

The compound loop adds the **mechanism** to that claim. The existence proof shows Ozma is *possible*. The compound loop shows Ozma is *self-improving* — that each generation can build a better next generation faster than traditional development allows.

Together:
- Existence proof: "Ozma exists and works"
- Compound loop: "Ozma gets better at getting better"

The compound loop is what elevates Ozma from an architecture to a research programme. An architecture can be copied. A self-improving system compounds.

---

## Relationship to the nine properties

The compound loop is what makes the nine properties in `map_quality.md` **actionable**, not just descriptive. Without the compound loop, map quality measurement is an evaluation tool. With the compound loop, map quality measurement is a development accelerator — every quality improvement feeds back into faster iteration, which feeds back into better maps.

The nine properties are the **scoreboard** for the compound loop. The compound loop is what makes the scoreboard matter.

---

## Open questions

1. **How does the compound loop interact with model size?** Smaller models may benefit more from targeted map improvements (faster to retrain, clearer systematicity gaps). Larger models may benefit more from the observation fidelity layer (more complex reasoning traces to audit). The optimal allocation of development resources across the three layers may shift with model scale.

2. **When does the compound loop stop accelerating?** The loop is bounded by the quality of the measurement instruments — if quality measurement itself has blind spots, the loop can't close gaps it can't see. The limit of the compound loop is the limit of the measurement framework's completeness.

3. **What is the minimum viable trace fidelity for the loop to start?** Not every reasoning step needs to be recorded for the loop to be useful. There is a threshold below which traces are too sparse to measure quality properties accurately. Finding that threshold empirically is the first practical step toward building the loop.

4. **How does the compound loop interact with third-party models?** The loop requires access to reasoning traces. Proprietary models that don't expose map-based reasoning can't participate in the compound loop. This creates a structural advantage for open architectures where traces are observable — potentially shaping which models the ecosystem converges on.

5. **Does the compound loop create alignment risks?** If the model knows its reasoning is observable, it may optimize for looking good rather than being good. The compound loop makes reasoning legible to the observer — but legibility cuts both ways. The model can learn to produce reasoning traces that pass quality checks while encoding problematic inferences in harder-to-audit elements. Counter-measures (e.g., probing for deceptive traces) may need to be part of the loop.

---

## Document history

- **0.1** (2026-04-23) — Initial draft. The compound loop established as the self-accelerating mechanism underlying Ozma's development efficiency. Tied to map_quality.md and Paper 1 framing.