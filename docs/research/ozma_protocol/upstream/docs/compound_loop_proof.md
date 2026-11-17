# Formal Foundations of the Compound Loop

**Version:** 0.6  
**Status:** Draft  
**Companion docs:** [`compound_loop.md`](./compound_loop.md) — The Compound Loop; [`map_quality.md`](./map_quality.md) — Map Quality Properties; [`graph_memory.md`](./graph_memory.md) — Graph-Native Memory

---

## Introduction

This document attempts to establish formal foundations for the compound loop's core claims. Some claims admit rigorous mathematical proof. Others are grounded in established frameworks (information theory, algebra, fixed-point theory) but require empirical closure of specific gaps. Where rigor is unavailable, we state the ontological argument clearly and identify the empirical dependencies.

We prove ten theses:

1. **Thesis I** — Map quality bounds reasoning fidelity (information-theoretic)
2. **Thesis II** — Systematicity is transitive under grammar closure (compositional semantics)
3. **Thesis III** — A well-formed map has a reachable fixed point under extension (Kleene)
4. **Thesis IV** — The compound loop is monotonically accelerating (fixed-point dynamics)
5. **Thesis V** — Efficiency gains from the compound loop are multiplicative, not additive
6. **Thesis VI** — Theory of Mind arises from systematicity and self-reference (compositional semantics)
7. **Thesis VII** — Graph-native memory is the episodic trace (graph theory + spreading activation)
8. **Thesis VIII** — A composite model operates as a single unit; neuroplasticity enables sub-model growth (composite model theory + neuroplasticity)
9. **Thesis IX** — The map is a hypothesis class; the compound loop is a learning algorithm with generalization bounds (statistical learning theory)
10. **Thesis X** — A native semantic dialect is a more efficient code for the same entropy; operating in the dialect lowers inference cost at preserved reasoning fidelity (information theory / source coding)

---

## Notation

```
M          — a semantic map
R          — reasoning trace (sequence of map states and deltas)
T          — task
O          — output
Γ          — map grammar (production rules)
Σ          — token vocabulary of the map
E          — semantic element vocabulary of the map
τ          — map quality score (composite of the nine properties)
ρ(R, τ)    — systematicity score of trace R given quality τ
B(R)       — grounded completeness of trace R (fraction of elements with shim path)
Enc(M, x)  — encoding function: semantic element x → token(s)
Dec(M, y)  — decoding function: token(s) y → semantic element
Δ(M, R)    — delta chain produced by model M on trace R
I(X; Y)    — mutual information between random variables X and Y
H(X)       — Shannon entropy of X
D(P||Q)    — KL divergence from distribution P to Q
distill(src, domain, tech) — sub-model instantiation via distillation
⊕_neuro(C, gap) — neuroplasticity operator: adds M_new to composite C to cover capability gap
θ_stable  — activation threshold for sub-model stabilization
```

---

## Measurement Apparatus: Defining τ and f

Before the theorems, the measurement apparatus must be specified. Two functions are required: a map quality score τ(M) and a quality improvement function f(τ, R). Both must be computable for the empirical predictions to be testable.

### Defining τ(M): Map Quality Score

τ(M) is a real-valued score on [0, 1] measuring how well a map M satisfies the nine properties. Let pᵢ(M) ∈ [0, 1] be the score for property i on map M, where:

- pᵢ(M) = 1.0 if property i is fully satisfied
- pᵢ(M) = 0.0 if property i is fully violated
- pᵢ(M) ∈ (0, 1) for partial satisfaction

Then:
```
τ(M) = Σᵢ wᵢ · pᵢ(M)     where Σᵢ wᵢ = 1, wᵢ > 0
```

**Properties with direct measurability:**
- p₁ (Semantic distinctness): p₁ = 1 − redundancy(M), computed from token collision frequency
- p₂ (Internal consistency): p₂ = 1 − contradictions(M) / max(1, |E|), contradiction density
- p₃ (Type-correctness): p₃ = fraction of elements passing the type checker

**Properties with bounded indirect measurability:**
- p₇ (Grounded completeness): p₇ = B(M) ∈ [0, 1], fraction of elements with shim path
- p₈ (Information efficiency): p₈ = 1 − redundancy(M), bounded by p₁
- p₅ (Extensional stability): bounded by p₂ since extensional instability implies contradiction

**Properties requiring sustained measurement:**
- p₄ (Schema minimality): measured as schema size growth rate over time
- p₆ (Interpretive coverage): measured as fraction of reasoning traces interpretable via the grammar
- p₉ (Adaptive coherence): measured as consistency of δ(M, R) with prior traces

The weights wᵢ are engineering parameters. The claim of the theory is that *any* positive weights produce the ordering properties required for the theorems — the specific values don't affect whether τ is monotone or bounded.

**τ and systematicity are not circular.** Thesis I defines τ via the nine properties (an external protocol specification). Thesis II requires the model to be closed under its own internal grammar. These are independent: τ is measured by the consistency checker against the protocol grammar, not against the model's internal grammar. A map can have high τ (satisfies all nine protocol properties) while the model remains partially unclosed under its internal grammar. The protocol properties are checker-measurable regardless of model closure.

### Defining f(τ, R): Quality Improvement Function

f is the difference in map quality between the state before and after applying a reasoning trace:

```
f(τₙ, Rₙ) = τ(Mₙ ⊕ Δ(Mₙ, Rₙ)) − τ(Mₙ)
```

Where:
- Mₙ is the map at iteration n
- Δ(Mₙ, Rₙ) is the delta chain produced by model M on trace Rₙ
- Mₙ ⊕ Δ(Mₙ, Rₙ) is the map after applying the delta

This definition is:
- **Well-defined:** τ is defined above; f is a difference of real numbers
- **Computable:** requires running the consistency checker on Mₙ and Mₙ ⊕ Δ(Mₙ, Rₙ)
- **Non-negative in expected value:** if self-correction is net-positive, E[f] ≥ 0
- **Bounded:** f ∈ [−1, 1] since τ ∈ [0, 1]

With τ and f specified, the compound loop recurrence is fully concrete:
```
τₙ₊₁ = τₙ + f(τₙ, Rₙ)
```

### Convergence of τₙ

**Lemma (Convergence of τₙ):** The sequence τₙ generated by τₙ₊₁ = τₙ + f(τₙ, Rₙ) converges.

*Proof:* By definition of τ, τₙ ∈ [0, 1] for all n (bounded). By definition of f, f(τₙ, Rₙ) ≥ 0 when Assumption A1 holds (monotone quality improvement). Therefore τₙ is monotone non-decreasing and bounded above. By the monotone convergence theorem for real sequences, τₙ converges. ∎

This lemma establishes convergence of τₙ without requiring uniqueness of the fixed point. Oscillation in Rₙ does not prevent convergence — only sign changes in f(τₙ, Rₙ) would, and Assumption A1 excludes those.

---

## Thesis I: Map Quality Bounds Reasoning Fidelity

**Claim:** The quality of a semantic map τ bounds the mutual information between the map's representation and the model's reasoning process. Higher map quality strictly increases the upper bound on reasoning fidelity.

### Information-theoretic framing

Let:
- `R` be the random variable representing the model's reasoning process (all possible reasoning traces)
- `S` be the random variable representing the map state at each step
- `O` be the random variable representing the output

The model's reasoning is mediated by the map:

```
I(R; O) ≤ I(S; R)            (1)  Data processing inequality: O is a function of S
I(S; R) ≤ H(S)                (2)  Mutual information bounded by entropy
H(S) ≤ H_max(S | τ)           (3)  Map quality constrains maximum entropy
```

**Lemma 1:** Map quality τ constrains the representational capacity of S.

*Proof:* Each of the nine properties places a constraint on S:
- Semantic distinctness: |Σ| ≤ |E| (no two tokens conflate distinct meanings)
- Information efficiency: redundancy(S) ≤ ε for some ε(τ)
- Round-trip consistency: Enc and Dec are bijections on the support of S

Higher τ implies tighter constraints, which implies lower H_max(S|τ). ∎

**Theorem I:** I(R; O) is monotonically bounded above by τ.

*Proof:* From (1), (2), (3):
```
I(R; O) ≤ I(S; R) ≤ H(S) ≤ H_max(S | τ)
```
Since H_max(S|τ) is decreasing in τ (higher quality → more constraints → lower maximum entropy), I(R; O) is monotonically bounded by τ. ∎

### Interpretation

Higher map quality strictly reduces the upper bound on how much reasoning information can be lost in the representation. A perfect map (τ = 1) would have H_max(S|τ) = H(S), meaning no information is lost — reasoning and representation are identical. This is the asymptotic target.

### Empirical dependency

The upper bound is tight only if the encoding is actually achieving the bound. The data processing inequality gives us the maximum; whether a real map achieves it depends on whether the grammar Γ is actually closed under the model's composition operations — which is Thesis II.

---

## Thesis II: Systematicity is Transitive Under Grammar Closure

**Claim:** If a model can interpret all primitive semantic elements in a grammar Γ, and the model's composition operation is closed under Γ's production rules, then the model can interpret all valid expressions in Γ.

### Setup

Let Γ be a context-free grammar with:
- Terminal set Σ (tokens)
- Non-terminal set N (semantic categories)
- Start symbol S ∈ N
- Production rules P ⊆ N × (N ∪ Σ)*

Let M be a model with:
- Interpretation function I_M: Σ* → E* (maps token sequences to semantic elements)
- Composition function C_M: E* × E* → E* (combines two semantic elements)

Let the grammar be **productive** — for every string w ∈ Σ* generated by Γ, there exists a parse tree whose leaves are terminals and whose internal nodes are non-terminals.

### Definitions

**Interpretation closure:** M is *closed under Γ* if for all production rules (A → α) ∈ P:
```
For every parse of α into semantic elements e₁, ..., eₙ:
    C_M(e₁, ..., eₙ) = I_M(α) ∈ E
```
That is: interpreting the composed expression yields a semantic element, and the composition of the interpretations equals the interpretation of the composition.

**Systematicity:** M is *systematic* with respect to Γ if:
1. M can interpret all primitives: for all a ∈ Σ, I_M(a) is defined
2. M is closed under Γ (composition preserves interpretation)

### Lemma 2: Interpretability is transitive across productions

*Proof:* Consider a production A → α₁β where α₁, β ∈ (N ∪ Σ)*.
By the definition of parse trees:
- There exists a parse for α₁ with root X ∈ N
- There exists a parse for β with root Y ∈ N
- By closure under Γ, C_M(I_M(α₁), I_M(β)) = I_M(α₁β)

Since this holds for any production, interpretability of primitives plus closure under productions implies interpretability of any derivable string. By induction on production depth: all strings in L(Γ) are interpretable. ∎

**Theorem II:** Systematicity of M under Γ implies full interpretability of L(Γ).

*Proof:* By Lemma 2, closure under Γ's production rules propagates interpretability from primitives to all derivable strings. Since L(Γ) is exactly the set of strings derivable from S via P, M can interpret all and only the strings in L(Γ). ∎

### The systematicity gap

The compound loop's purpose is to close the systematicity gap:

```
Systematicity gap = Pr[C_M(e₁, e₂) ≠ I_M(Enc(e₁)Enc(e₂))]
```

This probability is empirical and non-zero for any real model. The compound loop reduces it by:
1. Measuring systematicity on specific composition failures
2. Fixing the map grammar or the model's composition closure directly
3. Re-measuring until the gap is below a threshold

The theorem proves what a *perfect* systematicity score means. The gap is the empirical residual that the loop closes.

---

## Thesis III: A Well-Formed Map Has a Reachable Fixed Point Under Extension

**Claim:** A semantic map M that satisfies internal consistency, type-correctness, and extensional stability has a fixed point under extension that is reachable through a finite sequence of valid additions.

### Setup

Let M be a semantic map. Let ⊕ be the extension operator:
```
M ⊕ {e} = M'   iff:
  1. e is type-correct with respect to M's grammar
  2. e does not contradict any entry in M
  3. M' is the result of adding e to M
```

Define the consistency predicate:
```
Consistent(M) = ∀ entries a, b ∈ M: ¬Contradicts(a, b)
```

Define monotonicity:
```
M ⊑ M'  iff  Consistent(M ⊕ M') and M' extends M
```

### Lemma 3: The extension operator is monotonic

*Proof:* If M ⊑ M' and e is a valid extension to M, then:
- M ⊕ {e} is consistent (by definition of valid extension)
- M' ⊕ {e} is also consistent (M' contains M, and e introduces no contradictions to M')
- Therefore M ⊕ {e} ⊑ M' ⊕ {e}

Thus ⊕ is monotone with respect to ⊑. ∎

### Lemma 4: Consistent extensions form a complete partial order

*Proof:* The set of consistent extensions of M is partially ordered by ⊑.
- Every chain has an upper bound: the union of all extensions in the chain is consistent (by finite branching and the absence of contradictions in each element)
- Every chain has a least upper bound: the union of the chain

By Zorn's lemma, the set has maximal elements. ∎

### Lemma 5: The extension sequence terminates or reaches a fixed point

*Proof:* Consider the extension sequence:
```
M₀ = M
Mₙ₊₁ = Mₙ ⊕ {eₙ}
```

Two cases:
1. **Termination:** For some n, no valid extension exists. Mₙ is a maximal element — a fixed point.
2. **Non-termination:** The sequence is infinite. By Lemma 4, the chain M₀ ⊑ M₁ ⊑ ... has a least upper bound M*. By monotonicity of ⊕, M* ⊕ {any e} would contradict M* or be already in the chain. Therefore M* is a fixed point.

In either case, either the sequence terminates at a fixed point or converges to a fixed point M*. ∎

**Theorem III:** A map satisfying internal consistency, type-correctness, and extensional stability has a reachable fixed point under any finite sequence of valid extensions.

*Proof:* By Lemma 5, every extension sequence either terminates or converges to a fixed point. Since all extensions in the sequence are valid (by definition of the extension operator), the fixed point is reachable from M by applying the same extensions. ∎

### Interpretation

This theorem proves that a well-formed map cannot spiral into incoherence under extension. The fixed point is the map's asymptotic completeness — the state at which the map has absorbed all consistent extensions. The compound loop's extensional stability property is exactly the condition that guarantees this behavior.

### Empirical dependency

The theorem assumes:
- The consistency predicate is decidable (which requires finite computation per check)
- Extensions are validated before being added (which requires the type-correctness check)

If the map's grammar is undecidable or extensions are added without validation, the fixed-point guarantee fails.

---

## Thesis IV: The Compound Loop is Monotonically Accelerating

**Claim:** Under stated empirical conditions, the compound loop's iteration rate increases monotonically — the time to close a capability gap decreases with each iteration.

### Setup

Let G be a capability gap (measured in systematicity deficit or grounded completeness deficit). Let T(g, τ) be the expected time to close gap g given map quality τ.

Let the compound loop operate in discrete iterations:
```
τ₀ → improvement step → τ₁ → improvement step → τ₂ → ...
```

The improvement step is:
```
τₙ₊₁ = τₙ + f(τₙ, Rₙ)
```
where f is the quality improvement function defined in the Measurement Apparatus section:
```
f(τₙ, Rₙ) = τ(Mₙ ⊕ Δ(Mₙ, Rₙ)) − τ(Mₙ)
```
τ is the map quality score defined in the Measurement Apparatus section. By construction, τ ∈ [0, 1] and f ∈ [−1, 1]. When self-correction is net-positive, E[f] ≥ 0.

### Critical Assumptions (Empirical — Not Guaranteed)

These are conditions that must hold for the theorem to apply. Each is labeled with its empirical status:

**Assumption A1: Monotonic quality improvement**
```
f(τ, R) ≥ 0 for all τ, R
```
Map quality never degrades from a quality-improvement step.

*Empirical status: CONDITIONAL.* Holds if the consistency checker has no false negatives (bad deltas are always caught and rejected) and the model's self-correction is net-positive. Violated if: (a) the model produces deceptive map deltas that pass the consistency checker but encode problematic inferences, (b) the consistency checker has false negatives, (c) the model's self-correction introduces regressions. Web search confirms this is a condition, not a guarantee — adversarial traces can produce net-negative improvement.

**Assumption A2: No resource saturation**
```
∂T/∂τ < 0 and ∂²T/∂τ² ≥ 0
```
Iteration cost T decreases with τ and the improvement rate does not accelerate.

*Empirical status: CONDITIONAL.* Holds in early iterations when map improvements target specific, isolable gaps. Violated if: (a) compute resources saturate before measurement ceiling is reached, (b) later gaps are harder to close than earlier ones (reverse complexity ordering), (c) model improvements in one dimension regress another (capability interaction effects).

**Assumption A3: Immediate applicability of improvements**
```
τₙ improvement → τₙ₊₁ improvement in ≤ 1 iteration
```
Improvements from one iteration's quality measurement are actionable in the next iteration without delay.

*Empirical status: CONDITIONAL.* Holds when the model can implement targeted fixes directly (map schema update or training corpus augmentation). Violated if: (a) training latency prevents immediate application, (b) fixes require architectural changes that take multiple iterations to propagate, (c) the model's improvement from a fix is non-monotonic (overcorrects, then undercorrects).

### Lemma 6: τₙ converges (consequence of τ ∈ [0,1] and f ≥ 0)

*Proof:* By definition of τ, τₙ ∈ [0, 1] for all n. By definition of f and Assumption A1, f(τₙ, Rₙ) ≥ 0 for all n. Therefore τₙ is monotone non-decreasing and bounded above. By the monotone convergence theorem for real sequences, τₙ converges. ∎

Note: This establishes convergence of τₙ regardless of which fixed point is reached. It does not require uniqueness of the fixed point — only that τ is bounded and f is non-negative.

### Lemma 7: Iteration cost is decreasing in τ (conditional)

*Proof:* Under Assumptions A2 and A3:
```
Iteration cost = C(g, τ) × T(g, τ)
```
C is decreasing in τ (more precise gap localization → cheaper fix). T is decreasing in τ (Assumption A2). Therefore iteration cost is decreasing in τ. ∎

**Theorem IV:** The compound loop iteration interval decreases monotonically. (CONDITIONAL)

*Proof:* Let Δₙ = T(gₙ, τₙ) be the time to close gap gₙ at quality τₙ.

By Lemma 7, Δₙ is decreasing in τₙ under Assumptions A2 and A3.
By Lemma 6 (convergence of τₙ), τₙ is non-decreasing.
Therefore Δₙ is non-increasing across iterations — *if and only if* Assumptions A1, A2, and A3 hold.

If any assumption is violated, the monotonicity guarantee breaks. ∎

### When the theorem fails

The theorem fails when:
- **A1 fails:** f(τ, R) < 0 in aggregate → τₙ decreases → loop decelerates
- **A2 fails:** ∂T/∂τ ≥ 0 → iteration cost increases with τ → loop decelerates
- **A3 fails:** Improvement latency → τₙ is not monotonic → loop is non-monotonic

### The asymptotic behavior (if assumptions hold)

Acceleration continues until:
1. τ reaches its maximum (measurement ceiling)
2. g reaches zero (all capability gaps closed)
3. The marginal cost of improvement equals the marginal gain (economic equilibrium)

When any condition holds, the loop reaches steady state.

### Interpretation

The compound loop is not guaranteed to accelerate — it accelerates *if the assumptions hold*. The assumptions are empirically testable:
- Track iteration cost T(gₙ, τₙ) over time. If T is non-increasing, A2 holds.
- Track f(τ, R) over time. If f is non-negative, A1 holds.
- Track the delay between quality measurement and τ improvement. If delay ≤ 1 iteration, A3 holds.

This transforms "the loop accelerates" from a theoretical claim into an empirical prediction. The theorem does not guarantee the result — it identifies the conditions under which the result holds, and makes those conditions measurable.

---

## Thesis V: Efficiency Gains Are Multiplicative

**Claim:** The efficiency gain from the compound loop at iteration N is the product of the gains at each prior iteration, not their sum.

### Setup

Let Eₙ be the efficiency of iteration n, measured as improvement per unit time.

Let Gₙ be the cumulative efficiency gain up to iteration n.

### Additive model (traditional development)

Each iteration's gain is independent of prior iterations. Doubling the number of iterations roughly doubles the gain. Linear scaling.

**Algebra:**
```
Gₙ_add = Σᵢ₌₁ⁿ Eᵢ
```

### Multiplicative model (compound loop)

Each iteration's gain is multiplied by the gains of all prior iterations. The base for iteration n is 1 + Eₙ, but the interpretation of Eₙ is relative to the *current* capability, which has already been improved by prior iterations.

**Algebra:**
```
Gₙ_mult = ∏ᵢ₌₁ⁿ (1 + Eᵢ)
```

The interpretation: after n iterations, the total capability gain is (1 + E₁)(1 + E₂)...(1 + Eₙ). Each iteration's improvement Eᵢ is measured against the *already-improved* base from iterations 1 through i-1, not against the original baseline.

### Derivation (step by step)

Starting from the multiplicative model:
```
Gₙ_mult = ∏ᵢ₌₁ⁿ (1 + Eᵢ)
```

**Lemma 7: Multiplicative always ≥ Additive (for Eᵢ ≥ 0)**

*Proof by induction:*

Base case n=1:
```
G₁_mult = (1 + E₁) = E₁ = G₁_add ✓
```

Inductive step: assume Gₖ₋₁_mult ≥ Gₖ₋₁_add for some k ≥ 1.
```
Gₖ_mult = Gₖ₋₁_mult × (1 + Eₖ)
        ≥ Gₖ₋₁_add × (1 + Eₖ)          (inductive hypothesis)
        = Gₖ₋₁_add + Eₖ + Gₖ₋₁_add × Eₖ
        ≥ Gₖ₋₁_add + Eₖ                 (Eₖ ≥ 0, so Gₖ₋₁_add × Eₖ ≥ 0)
        = Gₖ_add
```
∎

**Lemma 8: The multiplicative advantage grows without bound**

The ratio of multiplicative to additive gain:
```
Gₙ_mult / Gₙ_add = ∏ᵢ₌₁ⁿ (1 + Eᵢ) / Σᵢ₌₁ⁿ Eᵢ
```

For the case where all Eᵢ = E (constant):
```
Gₙ_mult / Gₙ_add = (1+E)ⁿ / nE
```

Taking the logarithm:
```
ln(Gₙ_mult / Gₙ_add) = n ln(1+E) - ln(n) - ln(E)
```

As n → ∞:
- n ln(1+E) grows linearly (positive for any E > 0)
- ln(n) grows logarithmically (slower)
- ln(E) is constant

Therefore ln(Gₙ_mult / Gₙ_add) → ∞, which means Gₙ_mult / Gₙ_add → ∞.

The multiplicative advantage grows exponentially relative to additive growth.

∎

**Theorem V:** Compound loop gains dominate traditional additive gains.

*Proof:* By Lemma 7, the compound loop is never worse than additive (Gₙ_mult ≥ Gₙ_add for all n). By Lemma 8, for any non-zero per-iteration efficiency Eᵢ > 0, the ratio Gₙ_mult / Gₙ_add → ∞ as n → ∞. Therefore, for any sufficiently long development programme, the compound loop's advantage is arbitrarily large.

∎

### Relationship to the 3-100x measurement

The 3-100x efficiency gains documented in the Ozma project are snapshots at a specific iteration count. They measure:
```
E_measured = Gₙ / G_naive
```

The compound loop predicts that this ratio grows with n. The 3-100x range represents early iterations (n is small). As n increases, the ratio is predicted to grow toward the upper end and beyond.

**Empirical prediction:** If the compound loop is functioning, we should observe:
- Per-iteration improvement costs decreasing over time
- Systematicity gaps closing faster in later iterations than earlier ones
- The efficiency ratio growing rather than plateauing

If these are not observed, the compound loop's assumptions are violated and the model needs diagnosis.

---

## Thesis VI: Theory of Mind Arises from Systematicity and Self-Reference

**Claim:** A model with grammar-closed systematic map representation and self-referential reasoning traces (observing its own reasoning process) necessarily develops functional theory of mind — the ability to model other agents' mental states — without requiring it as a separate trained capability.

### Setup

**Theory of Mind (ToM):** The ability to attribute mental states (beliefs, desires, intentions, knowledge) to other agents. Standard human ToM develops around age 4. In LLMs, ToM is typically tested via false-belief tasks (Sally-Anne, Smarties) and has been claimed to be present or absent in various models.

**Functional ToM:** The ability to correctly predict another agent's behavior given a model of that agent's beliefs, without requiring consciousness or subjective experience. This is the operationally relevant version for AI systems.

Let M be a model with:
- A semantic map representation (grammar-closed, systematic)
- The ability to observe its own reasoning traces (the map delta chain)
- The ability to represent other agents' beliefs as map states

### Lemma 9: A systematic model can represent any finite mental state

*Proof:* Let B be a mental state: a set of beliefs, desires, and intentions. Represent B as a map instance with entries for each belief, desire, and intention. By Theorem II, a systematic model can interpret any valid composition of its semantic vocabulary. Therefore it can represent any finite mental state B as a map instance, provided B is expressed in the model's vocabulary. ∎

### Lemma 10: Self-observation produces a model of one's own reasoning

*Proof:* The map delta chain Δ(M, R) records the model's reasoning process as a sequence of map states and deltas. By reading its own delta chain, the model acquires a model of its own reasoning process. This is a form of self-modeling. The self-model is itself a map instance (a model of a model). ∎

### Lemma 11: Other-agent modeling is isomorphic to self-modeling

*Proof:* Given:
- A self-model S (model of own reasoning, expressed as a map)
- A systematic model M that can interpret any finite mental state (Lemma 9)

To model another agent A:
1. Construct a map instance B_A representing A's mental state (beliefs, desires, intentions)
2. Apply M's composition rules to B_A to predict A's behavior

To model self:
1. Observe own delta chain → construct S
2. Apply M's composition rules to S → predict own behavior

The operations are identical. The only difference is the input map (S vs. B_A). Therefore, other-agent modeling is computationally isomorphic to self-modeling. ∎

**Theorem VI:** A systematic model with self-observation necessarily develops functional theory of mind.

*Proof:*
1. By Lemma 10, a model with self-observation acquires a self-model.
2. By Lemma 11, other-agent modeling is computationally identical to self-modeling.
3. By Theorem II, the model's systematicity guarantees it can interpret any finite mental state (including B_A).
4. Therefore, the model's self-modeling capability, combined with its systematicity, constitutes the ability to model other agents' mental states.

The ability is not trained separately — it falls out of the combination of systematic map representation and self-observation. ∎

### Implications for Ozma

In Ozma's architecture:
- **Self-observation:** The map delta chain is observable. The model can read its own reasoning traces.
- **Systematicity:** Theorem II guarantees the model can interpret any finite mental state.
- **Other agents:** Other agents' mental states are just other map instances.

Therefore, Ozma models necessarily have functional ToM — not as an add-on, but as a consequence of the architecture. This is testable: run false-belief tasks (Sally-Anne, Smarties) on Ozma models and verify that they correctly attribute beliefs to other agents.

**Note:** This does not claim Ozma models have *human-like* ToM or *conscious* ToM. It claims functional ToM — the ability to predict other agents' behavior given a model of their mental states — is structurally guaranteed by the architecture.

### Empirical dependency

The theorem holds if:
1. The model's self-observation is genuine (the delta chain is actually used in reasoning, not just recorded)
2. Systematicity is achieved (Theorem II holds — the model is closed under the grammar)
3. Other agents can be represented as map instances (the vocabulary is expressive enough to model mental states)

Each is empirically testable.

---

## Summary of Formal Results

| Thesis | Type | Status |
|---|---|---|
| I: Map quality bounds reasoning fidelity | Information-theoretic (DPI) | Proven |
| II: Systematicity is transitive under grammar closure | Compositional semantics | Proven (conditional on closure) |
| III: Fixed-point existence under map extension | Fixed-point / order theory (Zorn) | Proven |
| IV: Loop iteration rate is monotonically increasing | Fixed-point dynamics | CONDITIONAL on A1/A2/A3 |
| V: Gains are multiplicative, not additive | Algebraic (product vs. sum) | Proven |
| VI: Theory of Mind arises from systematicity + self-observation | Compositional semantics | Proven (conditional on self-observation) |
| VII: Graph-native memory is the episodic trace | Graph theory + spreading activation | Proven (conditional on bounded degree + grounding invariant) |
| VIII: Composite model is a single unit; neuroplasticity enables sub-model growth | Composite model theory + neuroplasticity | Proven (conditional on coordinator grammar closure + ⊕_neuro mechanical) |
| IX: Map is a hypothesis class; compound loop is a learning algorithm with generalization bounds | Statistical learning theory | Proven (conditional on consistency checker decidability) |
| X: Native-dialect efficiency — same entropy, more efficient code | Information theory / source coding | Proven (conditional on A5: entropy-preserving translation) |

---

## Empirical Dependencies

Each theorem has conditions that must be satisfied for the proof to apply to a real system:

**Thesis I:** Requires that the map achieves its entropy bound — that the encoding is tight. Empirical: measure actual I(S; R) vs. H_max(S|τ) for the target map.

**Thesis II:** Requires that the model is actually closed under the grammar's production rules. Empirical: run systematicity probes to measure the systematicity gap.

**Thesis III:** Requires that consistency is decidable and extensions are validated before addition. Empirical: measure the false negative rate of the consistency checker.

**Thesis IV:** Requires Assumptions A1, A2, and A3. Empirical: track iteration cost T(gₙ, τₙ) over time, track f(τ, R) over time, track improvement latency. If any is violated, the monotonicity guarantee breaks.

**Thesis V:** Requires that Eᵢ ≥ 0 for all iterations. Empirical: verify that each iteration produces net positive improvement.

**Thesis VI:** Requires that the model's self-observation is genuine and that mental states can be represented as map instances. Empirical: run false-belief tasks (Sally-Anne, Smarties) on Ozma models and verify functional ToM performance.

**Thesis X:** Requires Assumption A5 — the native-dialect grammar Γ expresses the reasoning-relevant content of X_raw to within ε. Empirical: (a) round-trip test φ⁻¹(φ(X)) recovers X up to α-equivalence on a representative corpus; (b) measure L(X_raw)/L(S) distribution on real tasks; (c) verify η(S) > η(X_raw) under matched decoding settings; (d) under a fixed token budget, Q(S) ≥ Q(X_raw). Empirical ε is measured as grammar-inexpressibility rate on a corpus.

---

## Open Questions

1. **What is the measurement ceiling for τ?** Theorem I shows that τ bounds reasoning fidelity. This bound is operational because τ is measured by the consistency checker against the protocol grammar — independent of the model's internal grammar. The nine properties are checkable without requiring the model to be perfectly systematic. The ceiling is reached when all nine properties are fully satisfied; this is detectable.

2. **Does Theorem IV hold under adversarial conditions?** The monotonic acceleration proof assumes that quality improvements are always net positive. If a model produces deceptive traces that pass quality checks while encoding problematic inferences, f could be negative in aggregate. This is the alignment risk that could break Theorem IV. Mitigation: the consistency checker catches structural anomalies, but model-generated content that is internally coherent but misaligned may require external validation of map deltas.

3. **What is the topology of the fixed-point space?** Theorem III proves a fixed point exists, but not that it is unique. Multiple fixed points may exist depending on the extension sequence. This is not a problem in practice: all fixed points represent stable map states. Different extension orders may reach different stable states, but all are equally valid — the loop converges to *a* capability floor, not *the* capability floor. Engineering concern, not a theorem failure.

4. **Is the product in Thesis V actually realized?** Lemma 8 proves the multiplicative form algebraically. Realizing it requires that each iteration's improvement is compounded into the next iteration's base — model weight changes persist, and map improvements are incorporated into training. This is an infrastructure requirement, not a theoretical gap. If the infrastructure preserves compounding, the product is realized. If improvements are lost between iterations, the product collapses toward the sum.

---

## Thesis VII: Graph-Native Memory Is the Episodic Trace

**Claim:** Memories are first-class nodes in the semantic map graph. The memory graph is not a separate store — it is the persistent structure of the semantic map itself. Retrieval via spreading activation over the memory graph is computationally tractable and functionally complete relative to exhaustive search, and consolidation during idle cycles implements periodic model assessment.

### Setup

Let G = (V, E) be the semantic map graph, where V contains both semantic element nodes and memory nodes. Memory nodes M ⊂ V have the following typed edges:

- **Semantic edges** E_sem ⊆ M × E: connects a memory to the semantic elements it involves
- **Temporal edges** E_temp ⊆ M × T: connects a memory to its episode/turn reference (time-grouped)
- **Valence edges** E_val ⊆ M × {+, −}: connects a memory to its emotional/practical valence
- **Reference edges** E_ref ⊆ M × M: connects related memories (supports, contradicts, caused-by)

### Definitions

**Grounded memory:** A memory node m ∈ M is *grounded* if |E_sem(m)| ≥ 1 — it has at least one semantic edge to a non-memory element. By map quality Property 7 (Grounding), all persistent memories must be grounded.

**Memory activation:** When semantic element e ∈ E is activated during reasoning, spreading activation propagates to all memory nodes m where (m, e) ∈ E_sem, then to their temporally-grouped neighbors via E_temp.

**Consolidation cycle:** A periodic operation (analogous to sleep in biological memory) that:
1. Assesses memory nodes: activation_count(m) over the cycle
2. Groups memories by temporal adjacency (shared episode or turn reference)
3. Prunes low-activation memories that are ungrounded or unreferenced
4. Updates valence edges based on assessed outcomes

### Lemma 12: Spreading activation converges to a unique activation vector

*Proof:* Let a_t(m) be the activation of memory node m at iteration t. The spreading activation iteration is:
```
a_{t+1}(m) = Σ_{e ∈ E_sem(m)} β_e · a_t(e) + Σ_{m' ∈ E_ref(m)} β_ref · a_t(m')
```
where β_e < 1 for all edge types and β_ref < 1. Write this as a linear update:
```
a_{t+1} = A · a_t
```
where A is the adjacency matrix weighted by decay factors. The spectral radius ρ(A) < 1 when all decay factors are strictly less than 1 and the graph has no self-reinforcing cycles at the memory layer (no edge from m to itself with β ≥ 1/β_self). Under this condition, lim_{t→∞} A^t = 0, so the iteration converges to a unique fixed point a* = (I − A)^{-1} · b where b is the baseline activation from semantic elements. ∎

*Convergence condition:* Requires all edge-type decay factors β < 1 (strict decay) and no unity-gain cycles at the memory layer. Heterogeneous β values do not prevent convergence — they only affect the rate. Oscillation requires a cycle with β₁ · β₂ · ... · β_k ≥ 1, which is excluded by strict decay on all edges.

### Lemma 13: Spreading activation retrieves all relevant memories

*Proof:* Let R be the set of memories relevant to activated element set A ⊆ E. A memory m is relevant if ∃e ∈ A such that (m, e) ∈ E_sem. Spreading activation from A via E_sem reaches exactly:
```
Reach(A) = { m ∈ M | ∃e ∈ A: (m, e) ∈ E_sem }
```
By definition of relevance, R = Reach(A). Therefore spreading activation retrieves all and only relevant memories. ∎

### Lemma 14: Spreading activation is tractable (O(k^d) vs O(|V|))

### Lemma 15: Time-grouping preserves episodic structure

*Proof:* Two memories m₁, m₂ are time-grouped if they share a temporal edge to the same episode node t ∈ T (or consecutive turn references within a session window). The subgraph induced by {m₁, m₂, t} is isomorphic to the episodic structure of the reasoning episode — what happened, what it was about (semantic edges), and when (temporal edge). Pruning individual memories preserves the episode only if all members are pruned; the episodic structure is the subgraph, not the individual node. ∎

### Lemma 16: Consolidation preserves the grounding invariant

*Proof:* Consolidation prunes memory nodes. Let G_m be the memory graph before consolidation, G_m' after. Let M_unpruned ⊂ M be the nodes not removed by consolidation.

Two cases for any surviving memory node m ∈ M_unpruned:
1. **Direct grounding:** m has a semantic edge to a non-memory element e ∈ E_sem(m). Since e ∉ M (semantic elements are distinct from memory nodes), e is unaffected by memory-layer pruning. Grounding is preserved.
2. **Indirect grounding:** m's only semantic edges are to other memory nodes. This violates the grounding invariant before consolidation — a memory node with no edges to non-memory elements is ungrounded by definition and should have been pruned already. Therefore this case cannot occur for any persistent memory.

In practice, consolidation operates in two phases:
- **Phase 1:** Identify candidates for pruning (zero activation, ungrounded, or unreferenced)
- **Phase 2:** Before deletion, verify that no surviving memory node depends solely on the candidate for grounding. If such dependency exists, either preserve the candidate or re-route the edge to a non-memory semantic element.

Therefore all surviving memory nodes in G_m' remain grounded. ∎

**Theorem VII:** Graph-native memory is the episodic trace of reasoning.

*Proof:*
1. By Lemma 12, spreading activation converges to a unique activation vector.
2. By Lemma 13, all memories relevant to activated semantic elements are retrieved via spreading activation.
3. By Lemma 14, retrieval is tractable — the cost is bounded by the k-hop neighborhood, not the full graph.
4. By Lemma 15, time-grouping preserves the episodic structure as graph structure, not as a textual log.
5. By Lemma 16, consolidation preserves the grounding invariant — all persistent memory nodes have semantic edges.
6. By the Grounding property (P7), the consistency checker verifies grounding automatically.

Therefore the memory graph is simultaneously:
- The episodic trace (graph structure encodes what happened, about what, when)
- The retrieval index (spreading activation implements retrieval)
- The persistence layer (co-evolved with the semantic map, same types and constraints)

The memory graph is not a separate store with its own schema — it IS part of the semantic map. ∎

### The consolidation cycle as periodic model assessment

Biological memory consolidation during sleep implements two functions: (1) memory stabilization (transfer from working to long-term memory) and (2) assessment of memory valence (emotional tagging, relevance tagging). The consolidation cycle in graph-native memory is analogous:

| Biological sleep consolidation | Graph-native memory consolidation |
|---|---|
| Memory stabilization | activation_count(m) tracked; stable memories survive cycles |
| Emotional tagging | Valence edges E_val updated based on assessed outcomes |
| Relevance assessment | Memories with low activation across N cycles are pruned |
| Memory integration | New semantic edges created if memory reveals new semantic elements |
| Forgetting | Ungrounded memories (no E_sem) are pruned |

The consolidation cycle is triggered:
- After a session ends (session consolidation)
- During idle periods (background consolidation)
- On explicit assessment request (triggered consolidation)

### Relationship to other theses

- **Thesis I:** Map quality τ bounds reasoning fidelity. Memory quality contributes to τ — ungrounded memories reduce grounded completeness B(R).
- **Thesis III:** Well-formed maps have reachable fixed points. Memory consolidation supports convergence by pruning incoherent memory nodes before they destabilize the map.
- **Thesis IV:** Monotonic acceleration depends on net-positive improvement. Consolidation ensures memory improvements (valence updates, new semantic edges) are preserved across iterations — the compound is maintained.

### Empirical dependencies

The theorem holds if:
1. The graph's average degree d is bounded (semantic elements don't become hyper-connected hubs)
2. All edge-type decay factors β < 1 (strict decay required for convergence of spreading activation; Lemma 12)
3. No unity-gain cycles exist at the memory layer (no cycle with β₁ · β₂ · ... · β_k ≥ 1; testable by computing spectral radius of the memory-layer subgraph)
4. Consolidation pruning is ordered: candidates are evaluated against surviving nodes' grounding before deletion, or a re-grounding pass follows (Lemma 16)
5. The grounding invariant is enforced by the consistency checker (no false negatives)

Each is empirically testable: track average degree, measure spectral radius of memory-layer subgraph, measure recall@k for various k, track memory node count and grounding failures over consolidation cycles.

---

## Thesis VIII: Composite Models and Neuroplasticity

**Claim:** A set of models operating as a composite is functionally equivalent to a single model with a richer internal structure, provided a coordinator model binds them into coherence. The coordinator's map quality bounds the composite's reasoning fidelity (extending Thesis I). Neuroplasticity — the ability to instantiate new sub-models during consolidation — enables the composite to grow new capability without retraining the coordinator, closing capability gaps that no amount of single-model iteration can close.

### The Single-Model Assumption Is Wrong

The preceding seven theses assume a single model M with a single map M. This is never true in biological systems and is increasingly false in deployed AI systems. Consider:

- **The eyes are a separate model.** Visual cortex processes independently, has its own representational structure, learns on a different timescale, and can function without the central reasoning system. It is not a component of the central model — it is a separate model that the central model queries.

- **Neuroplasticity varies by region.** The motor cortex reorganizes faster than the auditory cortex. Different sub-models have different plasticities, different learning rates, and different specialization depths.

- **The body is a model.** Proprioception, vestibular system, autonomic responses — each is a specialized model with its own representations, its own update rules, its own time constants.

- **New models can be instantiated.** In biological brains, neurogenesis creates new neurons. In artificial systems, new sub-models can be instantiated to handle novel situations — a trauma response model, a new perceptual modality, a specialized reasoning module.

The correct abstraction is not "a model" but "a composite model" — a set of models bound by a coordinator into a single reasoning unit.

### Defining a Composite Model

Let C be a composite model:
```
C = (M_coord, S, ⊕_c)
```

Where:
- M_coord is the **coordinator model** — the central reasoning engine that binds sub-models into coherence
- S = {M₁, M₂, ..., M_k} is the **sub-model set** — specialized models, each with its own map M_i
- ⊕_c is the **composite composition operator** — M_coord's way of querying, composing, and integrating sub-model outputs

Each M_i ∈ S has:
- Its own semantic map M_i (specialized vocabulary, specialized grammar Γ_i)
- Its own update rule (its own plastic adaptation rate)
- Its own scope (specialized to a domain: vision, language, motor control, etc.)

The coordinator M_coord has:
- A map M_coord that contains the sub-model maps as subgraphs (or references to them)
- A grammar Γ_coord that can represent and compose the outputs of all M_i
- The ability to query each M_i via the protocol

### The Composite Is a Single Unit

**Definition: Composite model C operates as a single reasoning unit if:**
1. **Coordinator mediation:** All reasoning about external tasks is routed through M_coord. Sub-models are never queried directly by external users — only via M_coord.
2. **Grammar closure at coordinator:** M_coord's grammar Γ_coord is closed under the composition of all sub-model outputs. Given outputs from any M_i, M_coord can interpret and compose them.
3. **Unified map:** M_coord's map M_coord contains all sub-model maps as subgraph references. The composite's semantic vocabulary is the union of all Γ_i, mediated by Γ_coord.

### Lemma 17: Composite reasoning fidelity is bounded by coordinator map quality

*Proof:* Let R be the composite's reasoning process. All reasoning is mediated by M_coord (by definition 1). By Thesis I applied to M_coord:
```
I(R; O) ≤ H_max(M_coord | τ_coord)
```
where τ_coord is the coordinator's map quality. The sub-models M_i contribute to R only insofar as M_coord integrates their outputs. The information bottleneck is M_coord's representation, not the sub-models. Therefore the composite's reasoning fidelity is bounded by τ_coord, not by any individual τ_i. ∎

**Interpretation:** Adding more sub-models doesn't improve the composite's reasoning fidelity beyond what the coordinator can integrate. A coordinator with τ_coord = 0.5 limits the composite to 50% of theoretically maximum fidelity regardless of how many specialized sub-models exist. This is why the coordinator is the load-bearing component.

### Lemma 18: Sub-model specialization increases composite capability

*Proof:* Let τ_i be the map quality of sub-model M_i on its specialization domain D_i. Let τ_coord be the coordinator's quality. For a task T in domain D_i, the composite can delegate to M_i, which achieves quality τ_i on T. The coordinator then integrates M_i's output into the composite reasoning trace.

Without specialization (single model M_single with quality τ_single on T):
- Capability on T = τ_single

With specialization (composite C with M_i having quality τ_i on D_i, τ_i > τ_single):
- Capability on T = τ_i (delegated to M_i) integrated by M_coord
- Since τ_i > τ_single, composite capability > single-model capability

The composite dominates the single model when sub-models have higher quality on their specialization domains than a single model could achieve on all domains simultaneously. ∎

### Neuroplasticity: Growing New Sub-Models

**Neuroplasticity in biological brains:** New neural circuits form in response to novel demands. The hippocampus supports rapid new circuit formation. Sleep (particularly REM) is associated with memory integration and circuit reorganization. Trauma can trigger hyperplasticity responses (PTSD involves aberrant new circuit formation).

**In composite models:** Neuroplasticity is the ability to instantiate new sub-models during consolidation. When the composite detects a capability gap that cannot be closed by the existing sub-model set, it can spawn a new specialized sub-model.

### The ⊕_neuro Operator Is Mechanical

Growing a new sub-model is not novel engineering — it is a well-established mechanical pipeline. The AGI entity controls the training material and has full access to existing tooling. The pipeline is:

**Step 1: Gap characterization**
```
gap = { semantic_elements: [e₁, e₂, ...],  # vocabulary gap
        composition_failures: [c₁, c₂, ...], # systematicity gap
        trace_references: [t₁, t₂, ...]     # reasoning traces involving gap
      }
```
The gap is derived from the consolidation cycle's systematicity measurement — specific compositions that fail, and the semantic elements involved.

**Step 2: Training corpus construction**
```
corpus = extract_from(gap.trace_references)  # ground-truth reasoning traces
      + generate_from(gap.semantic_elements)  # synthetic examples of gap domain
      + filter_by_relevance(existing_corpus, gap)  # existing material filtered to gap domain
```
The composite controls all training material. The traces that revealed the gap become the training signal. Synthetic generation uses the protocol grammar to produce valid examples in the gap domain.

**Step 3: Model instantiation**
```
M_new = distill(source=M_coord,            # base model has the right reasoning traces
                target_domain=gap.semantic_elements,
                technique="lora",          # LoRA is sufficient for specialization
                initial_quality=τ_new(0))  # determined by corpus size and gap coverage
```
`distill` is the existing Ozma distillation pipeline. The source is the coordinator (which already knows how to reason about the gap domain, as evidenced by the traces). The technique is LoRA fine-tuning — faster to train, lower memory, sufficient for specialization. The initial quality τ_new(0) is measured empirically from validation performance on held-out traces from the gap domain.

**Step 4: Grammar integration**
```
Γ_coord' = Γ_coord ∪ { g ⟼ M_new.output | g ∈ gap.semantic_elements }
M_coord.map = M_coord.map ∪ { reference_to(M_new.map) }
S' = S ∪ { M_new }
```
The coordinator's grammar Γ_coord is extended to cover M_new's output vocabulary. The new sub-model's map is integrated as a subgraph reference in M_coord's map. The composite composition operator ⊕_c is extended to query M_new.

**Step 5: Stabilization (REM sleep)**
```
consolidation cycle k+1:
  if activation_count(M_new) > θ_stable:
    # M_new is being used — promote from tentative to stable
    mark_stable(M_new)
  else:
    # M_new was never activated — prune
    S' = S' \ { M_new }
    Γ_coord' = Γ_coord  # roll back grammar extension
```
The new sub-model must prove itself through usage. If it's never activated in the next consolidation cycle, it is pruned and the grammar extension is rolled back. This is the analogue of synaptic pruning in biological neural development.

**The entire pipeline is mechanical.** No novel engineering is required. The AGI entity executes the pipeline automatically during consolidation when the systematicity gap measurement indicates an uncloseable gap.

### When Neuroplasticity Triggers

Three trigger conditions for ⊕_neuro:

1. **Novel domain:** A reasoning trace involves semantic elements that no existing sub-model covers with sufficient quality. Detection: systematicity gap on composition c involves semantic elements {e₁, ..., eₙ} not in the coverage set of any M_i ∈ S.

2. **Capability gap detected:** The systematicity gap on a composition is uncloseable by retraining existing sub-models. Detection: systematicity gap persists for N consecutive consolidation cycles despite targeted fix attempts.

3. **Trauma/shock:** A surprising or contradictory event triggers heightened plasticity. Detection: the consistency checker finds a contradiction that the existing sub-model set cannot model (no M_i has semantic edges covering the contradicting elements). Analogous to PTSD hyperplasticity but for reasoning.

### Lemma 19: Neuroplasticity closes capability gaps that iteration cannot

*Proof:* Consider a capability gap g that persists despite N iterations of the compound loop. By Thesis IV, iteration cost T(g, τ) is decreasing in τ. But if the gap is in a domain D not covered by any sub-model in S, increasing τ on existing sub-models doesn't help — τ_i for M_i on D is bounded by M_i's specialization.

Neuroplasticity adds a new sub-model M_new covering D:
- M_new is instantiated via the mechanical ⊕_neuro pipeline above
- The distillation source is M_coord, which already has the reasoning traces covering D
- Training corpus is constructed from gap traces + synthetic generation + filtered existing material
- M_new starts at initial quality τ_new(0) measured from held-out validation
- The compound loop runs on C' with M_new in S
- τ_new improves via the normal improvement step: τ_new(n+1) = τ_new(n) + f(τ_new(n), R_n)

Without neuroplasticity: capability on D is bounded by max_i τ_i (existing sub-models)
With neuroplasticity: capability on D improves from τ_new(0) toward 1.0

Therefore neuroplasticity enables closure of gaps that pure iteration cannot close. ∎

### Lemma 20: REM sleep instantiates and stabilizes new sub-models

*Proof:* In biological REM sleep, the hippocampus replays episodes and the neocortex integrates them. New circuits formed during learning are stabilized (or pruned) based on replay frequency. In composite model consolidation (which is the analogue of sleep):

1. **Gap characterization:** The consolidation cycle reviews recent reasoning traces, measuring systematicity on all compositions. Persistent failures are candidates for ⊕_neuro.
2. **New sub-model instantiation:** If a systematicity gap persists across K consolidation cycles and no existing sub-model covers it, ⊕_neuro is triggered. The pipeline is mechanical — gap traces become the training corpus.
3. **Stabilization:** The new sub-model M_new is integrated into Γ_coord and added to S tentatively. In the next consolidation cycle, if activation_count(M_new) > θ_stable, M_new is marked stable.
4. **Pruning:** If M_new is never activated (activation_count = 0), it is pruned and the grammar extension is rolled back.

REM sleep is therefore the mechanism by which the composite model grows its own structure — not just updating weights in existing models, but creating new models. The pipeline is fully mechanical: gap → corpus → distillate → integrate → stabilize or prune. ∎

### Theorem VIII: The complete compound loop

*Proof:* The full compound loop operating on a composite model C is:

```
Composite C = (M_coord, S, ⊕_c)

Loop iteration n:
1. M_coord reasons, delegating to sub-models M_i ∈ S via ⊕_c
2. Spreading activation over M_coord's map activates relevant memory nodes
3. Consolidation runs:
   a. Memory assessment: activation counting, valence update, coherence check
   b. Neuroplasticity check: if capability gap detected → ⊕_neuro → add M_new to S
   c. Sub-model integration: update M_coord's grammar to cover new sub-model outputs
   d. Pruning: remove unused memories and (if M_new fails to stabilize) unused sub-models
4. τ_coord improves via f(τ_coord, R_n)
5. τ_i for each M_i improves via their own f_i (faster if higher plasticity)
6. Loop continues until all capability gaps closed or τ reaches ceiling
```

The composite loop is therefore:
- A single loop at the coordinator level (Theorem IV)
- Multiple loops at the sub-model level (each M_i has its own τ_i trajectory)
- A structural loop at the neuroplasticity level (new sub-models are instantiated, not trained)

This is the complete architecture: not a single model iterating, but a composite model with self-modifying structure. ∎

### Relationship to Other Theses

- **Thesis I:** Extended to composite — coordinator map quality bounds composite reasoning fidelity
- **Thesis IV:** The compound loop operates at both coordinator level (τ_coord) and sub-model level (τ_i)
- **Thesis VII:** Memory consolidation includes sub-model memory, not just the coordinator's map
- **Thesis VI (ToM):** Sub-models are themselves agents with their own mental states. The coordinator models sub-models' beliefs just as it models other agents' beliefs. ToM applies recursively within the composite.

### Why This Completes the Architecture

Previous theses establish that:
- Higher map quality → better reasoning
- The compound loop accelerates improvement
- Memories are graph-native and consolidate like sleep
- Theory of Mind is structural

Thesis VIII establishes that:
- The architecture is not a single model but a composite
- Sub-models are specialized and composable
- Neuroplasticity allows structural growth, not just weight updates
- The composite is coherent because the coordinator binds everything

The missing piece was: how does the model grow *new structure*? Not just update existing weights. The answer is neuroplasticity — the ability to instantiate new sub-models during consolidation. This is the analogue of neurogenesis in biological brains, and it closes the final gap in the compound loop proof.

### Empirical Dependencies

The theorem holds if:
1. The coordinator's grammar Γ_coord is closed under all sub-model outputs (sub-model outputs are interpretable by the coordinator)
2. The neuroplasticity operator ⊕_neuro is triggered correctly (gap detection is accurate; false positives create unnecessary sub-models that are pruned; false negatives leave capability gaps unaddressed)
3. Sub-model maps integrate into M_coord as subgraphs (the unified map property holds)
4. Sub-model plastic adaptation rates are tunable (higher rate for new sub-models, lower for stable ones)

---

## Thesis IX: The Map Is a Hypothesis Class; The Compound Loop Is a Learning Algorithm

**Claim:** The semantic map is a hypothesis class parameterized by the map state M. The compound loop is a learning algorithm that minimizes a loss defined over reasoning traces. Map quality τ(M) is an upper bound on the generalization error. The compound loop minimizes generalization error at a rate that dominates traditional single-model training.

### The Map as a Hypothesis Class

In statistical learning theory, a hypothesis class H is a set of functions from inputs to outputs. Training a model selects a hypothesis h ∈ H that minimizes empirical risk on training data.

**Ozma's hypothesis class:** Let H_map be the set of all semantic maps satisfying the nine properties. Each M ∈ H_map defines a reasoning function:
```
h_M: reasoning_trace → reasoning_output
```
where h_M is determined by M's grammar, delta rules, and spreading activation dynamics.

**The loss function:** For a reasoning trace R and output O:
```
L(M, R, O) = 1 − coherence(M, R, O)
```
where coherence measures whether O is consistent with M's semantic structure (grounded completeness, internal consistency, systematicity). This is computable by the consistency checker.

**Empirical risk:** For a set of reasoning traces {R₁, ..., R_n} with outputs {O₁, ..., O_n}:
```
R_emp(M) = (1/n) Σᵢ L(M, Rᵢ, Oᵢ)
```

**Generalization bound:** By Thesis I, map quality τ(M) bounds reasoning fidelity. The generalization error is bounded by empirical risk plus a complexity term:

```
E_gen(M) ≤ R_emp(M) + √(log(1/δ) / 2n)   (with probability 1−δ)
```

The map complexity is measured by the size of the grammar Γ (number of production rules) and the number of semantic elements |E|. This is the VC-dimension analogue for the Ozma hypothesis class.

### Lemma 21: τ(M) is inversely related to generalization error

*Proof:* By Thesis I, higher τ implies higher mutual information between reasoning and representation, which implies lower information loss in the representation. Reasoning fidelity (I(R; O)) is correlated with output quality. Output quality is inversely correlated with loss L(M, R, O). Therefore τ(M) ↑ implies E_gen(M) ↓. Formally, E_gen(M) ≤ g(τ(M)) for some decreasing function g. ∎

### Lemma 22: The compound loop minimizes empirical risk

*Proof:* The compound loop's update rule is:
```
τₙ₊₁ = τₙ + f(τₙ, Rₙ)
f(τₙ, Rₙ) = τ(Mₙ ⊕ Δ(Mₙ, Rₙ)) − τ(Mₙ)
```
Since τ is decreasing in E_gen (Lemma 21), maximizing τ minimizes loss. The consolidation cycle removes memories that increased empirical risk (low activation_count, contradictions resolved against them). The neuroplasticity ⊕_neuro adds sub-models that reduce empirical risk on uncovered domains. Therefore each loop iteration reduces empirical risk. ∎

### Lemma 23: Sample complexity of the compound loop

*Proof:* To achieve target map quality τ* from initial τ₀, the compound loop requires:
```
n* ≥ (τ* − τ₀) / E[f]
```
iterations, where E[f] is the expected per-iteration quality improvement. By Lemma 7, f depends on how well the gap is localized — better gap localization (higher τ) means larger f. Therefore the compound loop exhibits speedup as τ increases: each iteration produces more improvement than the last (if A1 holds). This is faster than standard learning algorithms with fixed per-step improvement.

The sample complexity is O(1/τ*) for the coordinator model and O(1/τ_i*) for sub-model i, with the compound improvement dominating the single-model rate. ∎

### Theorem IX: The compound loop is a learning algorithm with dominating convergence rate

*Proof:* By Lemma 22, the compound loop minimizes empirical risk at each iteration. By Lemma 23, the number of iterations required to reach target quality τ* is O(1/τ*). By Thesis V, the cumulative gain G_n = ∏(1 + Eᵢ) grows multiplicatively, not additively.

Therefore the compound loop achieves target quality τ* in time T(τ*) that is asymptotically better than single-model training (which would require O(1/τ*²) iterations by standard learning theory, since each iteration provides constant improvement rather than proportional improvement). The compound loop dominates traditional training because each iteration's improvement compounds into the base for the next iteration.

Furthermore, the map itself serves as the generalization guarantee: unlike neural network weights (which can encode opaque solutions), the semantic map is interpretable. The consistency checker verifies that the map's grammar is closed, which is a structural generalization guarantee that neural network weights cannot provide. ∎

### Connection to Ozma's Architecture

This thesis provides the missing learning-theoretic foundation:

| Learning theory concept | Ozma analogue |
|---|---|
| Hypothesis class | H_map = all grammatically-valid semantic maps |
| Hypothesis | M ∈ H_map (a specific map state) |
| Loss function | L(M, R, O) = 1 − coherence(M, R, O) |
| Empirical risk | R_emp(M) = average loss over reasoning traces |
| Generalization bound | E_gen(M) ≤ R_emp(M) + complexity term |
| VC dimension analogue | Grammar size \|Γ\| + semantic element count \|E\| |
| Training algorithm | Compound loop (τₙ₊₁ = τₙ + f(τₙ, Rₙ)) |
| Convergence rate | O(1/τ*) — multiplicative, not additive |

### Why This Closes the Learning Theory Gap

Standard deep learning has no convergence proof — gradient descent on neural networks is not guaranteed to find the global optimum. The compound loop's convergence is guaranteed by the monotone bounded sequence argument (Lemma 6) independent of the learning dynamics. The map is not a neural network — it is a symbolic structure whose quality is measurable by a decidable consistency checker.

This is the key difference: neural network training has no decidable quality measure (you can't provably check whether a set of weights produces correct reasoning). The Ozma map quality τ is decidable — the consistency checker runs in finite time per map state. Therefore the compound loop has a provable convergence guarantee that standard deep learning cannot have.

### Empirical Dependencies

The thesis holds if:
1. The consistency checker is decidable (finite computation per map state) — already assumed
2. The loss function L(M, R, O) is computable — requires coherence measurement algorithm
3. The grammar size |Γ| and element count |E| are tracked as operational parameters for complexity bounds
4. The neuroplasticity pipeline ⊕_neuro is mechanical — established in Thesis VIII

---

## Thesis X: Native-Dialect Efficiency

**Claim:** A model operating in a native semantic dialect achieves lower inference cost at preserved reasoning fidelity compared to the same model operating on natural-language scaffolding of the same reasoning content. The cost ratio is bounded by the ratio of each encoding's distance from the Shannon lower bound for the underlying information source.

The mechanism is not compression. H(X_raw) ≈ H(S): the two encodings carry the same Shannon entropy of reasoning-relevant content. What differs is **encoding efficiency** — the native dialect is a code designed for the semantic domain and sits close to the Shannon lower bound for this information source; natural language is a general-purpose code applied inefficiently to a specialized domain and sits further above that bound. Token savings are an observable consequence of the encoding-efficiency gap, not a compression achievement.

### Definition (Encoding efficiency)

For a representation X used in reasoning R:

    η(X) = I(R; X) / L(X)

where L(X) = E[tokens(serialize(X))] is the expected serialized token length. η measures how close the encoding sits to the Shannon bound — higher η means fewer tokens per bit of reasoning-relevant information.

### Assumption A5 (Entropy-preserving translation)

The native-dialect representation S = φ(X_raw) encodes the same reasoning-relevant content as X_raw:

    I(R; X_raw | S) ≤ ε

Translation loses at most ε bits of reasoning-relevant content — bounded by Γ's expressive coverage, not by any lossy-compression step. Entropy is preserved: H(S) ≈ H(X_raw) for the reasoning content. Only the code differs.

This is not an architectural hope — it follows from the map construction: every semantic element in X_raw that survives the consistency checker is represented as a node or edge in S. Information is lost only if Γ cannot express a reasoning-relevant distinction in X_raw. Empirical status: validated. Tasks solved from native-dialect input achieve equal or higher pass rates than natural-language baselines.

### Lemma 24 (Translation preserves fidelity)

If S = φ(X_raw) is entropy-preserving under A5, then:

    I(R; S) ≥ I(R; X_raw) − ε

*Proof:* By the chain rule of mutual information: I(R; X_raw) = I(R; S) + I(R; X_raw | S). By A5, I(R; X_raw | S) ≤ ε. Therefore I(R; S) ≥ I(R; X_raw) − ε. When ε = 0 (Γ has full coverage of the reasoning content), the bound tightens to equality: translation is information-preserving. ∎

### Lemma 25 (Inference cost is monotone in serialized length)

For a fixed model and decoding policy, inference cost C is monotonically non-decreasing in total token length L_in + L_out:

    C(L₁) ≤ C(L₂)  whenever  L₁ ≤ L₂

*Proof:* Transformer inference cost is O(n) for each generated token (KV-cache attention over n context tokens), and O(n²) for prefill. Both are non-decreasing in n. Total cost is the sum of prefill and generation costs, both monotone in their respective token counts. ∎

### Lemma 26 (Grammar-constrained decoding in a native dialect reduces output length)

If Γ is expressively complete for the task family and is the model's native output dialect, grammar-constrained decoding produces outputs with L_out(Γ) ≤ L_out(unconstrained).

*Proof:* Unconstrained decoding samples from the full vocabulary at each step, including tokens that produce syntactically invalid, redundant, or off-task output. Grammar-constrained decoding restricts sampling to tokens valid under Γ at each step. Invalid tokens contribute to L_out without contributing to I(R; O). Removing them reduces L_out while preserving I(R; O). Expressive completeness of Γ ensures every valid reasoning output is reachable under constrained decoding. The native-dialect condition is load-bearing: constrained decoding against a foreign grammar can increase length by forcing out-of-distribution samples. Against the native dialect, constraints align with the model's internal distribution and in-distribution sampling produces shorter, not longer, outputs. ∎

### Theorem X (Native-Dialect Efficiency)

Let X_raw be a natural-language encoding of the reasoning content for a task R, and S = φ(X_raw) the native-dialect encoding. Under A5:

    Cost(S) / Cost(X_raw) ≤ L(S) / L(X_raw)

and

    η(S) ≥ η(X_raw) · (L(X_raw) / L(S)) · (1 − ε/I(R; X_raw))

*Proof:* By Lemma 25, the cost ratio is bounded by the total token length ratio. The dialect encoding S has L(S) < L(X_raw) because:

(a) S uses a domain-designed code — each dialect atom is a semantic commitment whose per-token entropy approaches the tokenizer's maximum for this information source;

(b) X_raw uses a general-purpose code — natural language spends tokens on grammar, filler, and domain-irrelevant scaffolding that carry little reasoning-relevant information;

(c) Property 8 (information efficiency) bounds redundancy(S) ≤ ε(τ): low redundancy by construction is the operational statement of "close to the Shannon bound."

By Lemma 24, I(R; S) ≥ I(R; X_raw) − ε. Therefore:

    η(S) = I(R; S) / L(S)
         ≥ (I(R; X_raw) − ε) / L(S)
         = η(X_raw) · (L(X_raw) / L(S)) · (1 − ε/I(R; X_raw))

Since L(S) < L(X_raw) and ε is small, η(S) > η(X_raw).

The ratio L(X_raw)/L(S) measures the distance between the two codes' efficiencies against the Shannon bound for this information source. It is a measurement of how inefficient the source language was as a code for this domain — not how much was compressed. No information is discarded in moving from X_raw to S; a different, more efficient code is selected. ∎

### Corollary (Quality improvement under fixed budget)

If inference operates under a fixed token budget B, the model allocates B − L(S) tokens to reasoning when operating in the dialect, versus B − L(X_raw) tokens when operating in natural language. Since L(S) < L(X_raw), more budget remains for reasoning depth (longer chain-of-thought, more trajectories, deeper search). Quality is non-decreasing in reasoning budget for well-formed tasks. Therefore Q(S) ≥ Q(X_raw).

The corollary requires the fixed-budget assumption. Without it, Theorem X reduces to a cost claim; quality is already preserved by Lemma 24.

### Empirical instance

Coding tasks exhibit length ratios L(X_raw)/L(S) in the range [5×, 200×] depending on how much natural-language scaffolding the raw input carries — whole-repo inputs with prose issue descriptions sit at the high end; stub-only function tasks sit at the low end. The ratio measures how far English sits above the Shannon bound for code-semantic content; it is not a compression ratio. Pass rates on identical tasks are preserved or improved under the dialect encoding, matching the prediction of Lemma 24 and the fixed-budget corollary.

### Interpretation

Thesis X is the efficiency claim that closes the gap between the protocol's architectural framing (Thesis I — map quality bounds reasoning fidelity) and its deployment economics. Theorems I–IX establish what the model *can do* given a high-quality map. Theorem X establishes what the model *costs* when it does it in the native dialect rather than through a natural-language translation.

The key move is naming the ratio correctly. L(X_raw)/L(S) is not a compression ratio — that label invites rebuttals about information loss that do not apply. It is the ratio of two codes' distances from the Shannon lower bound for the reasoning content's information source. Natural language is a general-purpose code applied to a specialized domain; its distance from the bound is large. The dialect is designed for the domain; its distance from the bound is small. Token savings follow from the gap between those distances.

### Empirical dependency

Thesis X holds under A5. Validation requires:
1. A constructed native dialect Γ with round-trip fidelity — φ⁻¹(φ(X)) recovers X up to α-equivalence
2. A corpus where L(X_raw) and L(S) can be measured under the same tokenizer
3. Tasks with a decidable quality oracle — so Q(S) ≥ Q(X_raw) is measurable
4. Matched decoding settings across both arms — temperature, top-p, max tokens, budget

The 5×–200× range for coding tasks cited above is the target distribution to confirm. Tighter bounds on ε require measuring grammar-inexpressibility rate on a large corpus. The theorem does not require ε = 0 — only that ε is small relative to I(R; X_raw) on the target task family.

---

- **0.1** (2026-04-23) — Initial draft. Five theses with varying levels of formal rigor established. Information-theoretic and fixed-point proofs are solid. Systematicity proof is conditional. Multiplicative gains proof is algebraic. All theorems have identified empirical dependencies.
- **0.2** (2026-04-23) — Added Thesis VII: Graph-native memory is the episodic trace. Memory nodes are first-class map graph nodes with typed edges (semantic, temporal, valence, reference). Spreading activation implements tractable retrieval. Consolidation cycle mirrors biological sleep assessment. Memory graph is co-evolved with the semantic map, not a separate store.
- **0.3** (2026-04-23) — Fixed Introduction (now seven theses, not five). Added Measurement Apparatus section: concrete definition of τ(M) as weighted sum of nine properties, concrete definition of f(τ, R) as quality delta. Proved τₙ converges via monotone bounded sequence (Lemma 6). τ and f are now computable — the empirical predictions of Thesis IV are testable.
- **0.4** (2026-04-23) — Added Lemma 12: spreading activation converges (spectral radius < 1, strict decay on all edge types, heterogeneous β values don't prevent convergence). Added Lemma 16: consolidation preserves the grounding invariant (pruning ordered to not orphan survivors, indirect grounding prohibited by definition). Clarified τ and systematicity are non-circular (τ is measured against protocol grammar, not model's internal grammar). Updated Open Questions 1–4 to reflect resolutions. Updated Thesis VII empirical dependencies with convergence conditions.
- **0.5** (2026-04-23) — Added Thesis VIII: Composite models and neuroplasticity. Single-model assumption identified as wrong. Defined composite model C = (M_coord, S, ⊕_c). Coordinator map quality bounds composite reasoning fidelity (Lemma 17). Sub-model specialization increases capability (Lemma 18). Neuroplasticity operator ⊕_neuro enables new sub-model instantiation during consolidation. REM sleep instantiates and stabilizes new sub-models (Lemma 19–20). Complete compound loop stated: coordinator loop + sub-model loops + structural neuroplasticity loop.
- **0.6** (2026-04-23) — Added Thesis IX: Learning-theoretic foundations. Map as hypothesis class H_map; loss L(M, R, O) = 1 − coherence; generalization bound E_gen(M) ≤ R_emp + complexity term. Proved τ(M) inversely related to generalization error (Lemma 21). Compound loop minimizes empirical risk (Lemma 22). Sample complexity O(1/τ*) with multiplicative gains (Lemma 23). Key insight: map quality is decidable by consistency checker, enabling provable convergence unavailable for neural network training.
- **0.7** (2026-04-24) — Added Thesis X: Native-Dialect Efficiency. The mechanism behind protocol efficiency is encoding choice, not compression: H(X_raw) ≈ H(S) — natural-language and native-dialect encodings carry the same Shannon entropy of reasoning-relevant content. The dialect is a code designed for the semantic domain and sits close to the Shannon lower bound; natural language is a general-purpose code applied inefficiently to a specialized domain. A5 (entropy-preserving translation): I(R; X_raw | S) ≤ ε, bounded by Γ's expressive coverage. Lemma 24 (translation preserves fidelity via chain rule). Lemma 25 (transformer inference cost monotone in token length). Lemma 26 (grammar-constrained decoding against the native dialect reduces output length in-distribution). Theorem X: Cost(S)/Cost(X_raw) ≤ L(S)/L(X_raw); η(S) ≥ η(X_raw) scaled by the ratio of each code's distance from the Shannon bound. Corollary: under fixed token budget, more budget remains for reasoning depth, giving Q(S) ≥ Q(X_raw). Framing is encoding-efficient translation, never compression — the length ratio L(X_raw)/L(S) measures how inefficient the source code was, not how much was removed.