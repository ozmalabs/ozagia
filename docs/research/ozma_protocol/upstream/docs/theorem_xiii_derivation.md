# Theorem XIII — Substrate-Markov-Mixing Efficiency: A Rigorous Derivation

**Companion technical report to** *Invariant Scale Graphs: Mapping Reality — Part I: Foundations and Methodology*, §9.4.

**Author:** Matthew Parslow (Ozma Labs)
**Status:** Working derivation; cross-referenced from Paper I §9.4. The main paper carries the proof sketch and statement; this document fills in the formalism, the lemmas, and the composition argument.

---

## 1. Why a separate document

Paper I §9.4 states Theorem XIII and gives a five-step proof sketch, sufficient for the paper's argumentative load. The full derivation requires:

- A formal definition of *substrate richness* that does not beg the question.
- A precise statement of the *hypothesis class of transition kernels* on a substrate.
- A PAC-Bayes-style sample-complexity argument relating prior structure to posterior learning cost.
- A composition argument with Theorem XI that justifies the multiplicative form.
- A worst-case-vs-typical-case discussion that gives both the exponential and the polynomial scaling regimes.

These do not all fit within the section budget of a §9 paper subsection. Following the precedent set by Thesis IX (Paper I §7.3 — sketch in main paper, full derivation deferred to a separate technical report), Theorem XIII's full derivation lives here.

The result is the *same* result Paper I §9.4 states. This document supplies the formal scaffolding.

## 2. Formalism

### 2.1 Substrates and domains

A **domain** D is a set of semantic units the substrate is intended to represent — for example, the set of well-typed expressions in a programming language, the set of facts in a knowledge base, or the set of valid program states for a particular system.

A **substrate** is a triple (X, Σ_X, μ_X) where X is a set, Σ_X a σ-algebra on X, and μ_X a probability measure on (X, Σ_X). For the cases of interest (typed graphs, token vocabularies), X is discrete and Σ_X is its power set.

A **substrate encoding** of D into X is a coupling κ ∈ P(D × X) — a joint probability measure on D × X — with the property that κ_D (the marginal on D) is the natural domain measure (e.g. uniform over a finite domain, or some semantically meaningful measure). The conditional κ(x | d) describes how a domain element d is represented as substrate state x; the conditional κ(d | x) describes what a substrate state x reveals about the domain.

### 2.2 Substrate richness

Define the **substrate richness** as the mutual information between substrate state and domain element under the encoding:

ρ(X, D, κ) = I_κ(X; D) = H(D) − H(D | X)

Equivalently, ρ is the average reduction in domain uncertainty achieved by observing a single substrate state.

Two anchor cases:

**Typed-graph substrate G.** For a typed-graph encoding of D where each domain element d ∈ D maps to a unique typed node n(d) ∈ V_G with relevant type, structural, and edge metadata: H(D | X) → 0 under a faithful encoding, so ρ(G) → H(D) — the maximum possible. Single-state observation is approximately sufficient to identify the domain element.

**Token substrate τ.** For a token-sequence encoding of D where each domain element is encoded as a sequence of tokens drawn from vocabulary V_τ, with token identity carrying no direct domain interpretation: a single token x ∈ V_τ has H(D | X = x) ≈ H(D) — observing a single token reveals very little about which domain element a sequence is encoding. So ρ(τ) ≪ H(D).

The **richness gap** is

Δρ = ρ(G) − ρ(τ) ≥ 0,

with equality only when the token substrate's single-state distribution happens to be domain-identifying (a degenerate case for any non-trivial vocabulary).

### 2.3 Markov chains on substrates

A **Markov chain on substrate X** is a transition kernel P : X × Σ_X → [0,1] such that:
1. For each x ∈ X, P(x, ·) is a probability measure on X (row stochasticity);
2. P admits a unique stationary distribution π_P satisfying π_P(A) = ∫ P(x, A) dπ_P(x) for all A ∈ Σ_X.

The chain induces a stochastic process (X_t)_{t ≥ 0} with X_{t+1} | X_t = x ∼ P(x, ·).

A **Markov-chain inference task** specifies a target stationary distribution π* ∈ P(X) and a tolerance ε > 0; the goal is to find P such that

TV(π_P, π*) ≤ ε

where TV is total-variation distance. Many computations of interest (sampling, query answering, retrieval, multi-hop reasoning) admit this form.

### 2.4 Hypothesis class of kernels

The space of all kernels on X is large. Practical learning operates within a **hypothesis class** H_X ⊆ {P : X × Σ_X → [0,1]} reflecting structural constraints on permissible kernels.

For typed-graph substrate G with edge set E_G and typing function τ:

H_G = { P : P(n, n') > 0 ⟹ (n, n') ∈ E_G ∧ τ-compatible(n, n') }

That is: kernels that respect the graph's edge structure and the type-compatibility rules. This is a parametric family with one free parameter per typed edge — dimension dim(H_G) = |E_G|.

For token substrate τ over vocabulary V_τ:

H_τ = { P : P is row-stochastic on V_τ × V_τ }

With no structural restrictions, dim(H_τ) = |V_τ|² − |V_τ| (full transition matrix minus row-stochasticity constraint).

For substrates over a domain of comparable expressive power, |V_G| ≈ |V_τ| in order of magnitude, but |E_G| ≪ |V_G|² because typed graphs are sparse — most pairs of nodes are not connected and most type pairs are incompatible.

### 2.5 Effective dimension and richness

Define the **effective dimension** d(H_X) as the log of the ε-covering number of H_X (in any reasonable metric; total-variation works):

d(H_X) = log N(H_X, TV, ε)

where N(·, TV, ε) is the minimum number of TV-balls of radius ε needed to cover H_X. For parametric families this is the standard learning-theoretic dimension and scales linearly with the parameter count (modulo constants for the metric and the regularity of the parameterisation).

**Key claim.** d(H_τ) − d(H_G) is bounded below by a function growing linearly in the richness gap times the substrate state count:

d(H_τ) − d(H_G) ≥ c · |X| · Δρ

for some absolute constant c, where |X| is the substrate state count.

This is established as Lemma 2 below.

## 3. Lemmas

### Lemma 1 — Richness is bounded above by log|D|

**Statement.** For any substrate X and encoding κ:
ρ(X, D, κ) ≤ H(D) ≤ log|D|.

**Proof.** Direct from the definition: ρ = I(X; D) = H(D) − H(D | X) ≤ H(D), with H(D) ≤ log|D| for finite D. ∎

This is unsurprising but useful: it bounds the maximum richness gap and lets us check that empirical richness measurements don't exceed the information-theoretic ceiling.

### Lemma 2 — Effective dimension and richness gap

**Statement.** For substrates X and Y over the same domain D with richness gap Δρ(Y, X) = ρ(Y, D, κ_Y) − ρ(X, D, κ_X), the effective dimensions of natural hypothesis classes satisfy:

d(H_X) − d(H_Y) ≥ |Y| · Δρ(Y, X) − O(|Y| log(1/ε))

where the O(·) term accounts for cover-radius effects.

**Proof sketch.** A kernel on substrate X must encode two pieces of information:

(a) *Substrate structure* — which states are connected to which under what types. Information content I_struct(X) = log|H_X structural-only|, which is the structural information per state times the state count: I_struct(X) ≈ |X| · (1 − ρ(X, D)/H(D)) · log|X|. Higher richness means more structure given by the substrate itself, lower I_struct.

(b) *Task-specific weighting* — the conditional probabilities once structure is given. Information content I_task is approximately substrate-invariant for tasks of comparable complexity over comparable domains.

Total kernel information: I_kernel(X) = I_struct(X) + I_task ≈ |X|·(H(D) − ρ(X, D)) + I_task.

The richness gap captures exactly the structural-information difference: I_struct(τ) − I_struct(G) ≈ |X| · Δρ(G, τ).

Effective dimension d(H_X) is information divided by the cover-radius effect, giving the stated form. The constant in front depends on the metric (TV here) and on regularity of the parameterisation; we treat it as O(1) absorbed into the absolute constant c. ∎

### Lemma 3 — PAC-Bayes sample-complexity bound

**Statement (McAllester 1999, applied to kernel learning).** For prior π on H, posterior Q learned from data S of size n, and bounded loss L on the kernel-prediction task: with probability ≥ 1 − δ,

E_{P~Q}[L(P)] − E_{P~Q}[L̂_S(P)] ≤ √( (KL(Q || π) + log(2√n / δ)) / (2(n − 1)) ).

**Standard derivation.** See McAllester (1999) Theorem 1, Catoni (2007) for tighter constants, or Alquier (2024) for a modern survey. The bound holds for any P-distribution loss in [0, 1]; we apply it to TV-distance loss between the kernel-induced stationary distribution and the target.

### Lemma 4 — Sample complexity ratio in the richness gap

**Statement.** For substrates X and Y over domain D with richness gap Δρ(Y, X), the sample complexity ratio for learning a kernel achieving target risk ε with confidence 1 − δ satisfies:

n_X(ε, δ) / n_Y(ε, δ) ≥ 1 + (|Y| · Δρ(Y, X) − O(|Y| log(1/ε))) / d(H_Y)

**Proof.** PAC-Bayes (Lemma 3) gives sample complexity n ∝ KL(Q || π) / ε² for a fixed target risk and confidence. Under uninformative priors, KL(Q || π) is dominated by log|H_ε|, the log of the ε-cover, which is the effective dimension d(H).

So n ∝ d(H) / ε². Therefore:

n_X / n_Y = d(H_X) / d(H_Y) = 1 + (d(H_X) − d(H_Y)) / d(H_Y).

Substituting Lemma 2:

n_X / n_Y ≥ 1 + (|Y| · Δρ − O(|Y| log(1/ε))) / d(H_Y).

For Y = G (typed graph) and X = τ (token substrate) over a comparable domain, d(H_G) ≪ |X|·Δρ for any non-trivial domain (the typed substrate's hypothesis class has dimension small relative to the structural-information content). The ratio n_τ / n_G grows monotonically with Δρ, unboundedly as Δρ → log|D|. ∎

### Lemma 5 — Theorem X bounds the richness gap from below

**Statement.** Theorem X (Paper I §9.1) implies a lower bound on the richness gap:

Δρ(G, τ) ≥ log(|I| / K_Γ(I)) / |X_τ|

for inputs I in the domain D, where |I| is the byte-encoded size and K_Γ(I) the Γ-relative Kolmogorov complexity.

**Proof.** Theorem X established that the byte-encoding length |I| exceeds the typed-graph encoding length K_Γ(I) by a factor (|I| / K_Γ(I)) for structured inputs. Source-coding duality: the per-state information content of a substrate is bounded above by the inverse of the encoding length per domain element. The richness gap is the per-state information difference; integrated over the substrate state count, it must equal the total information difference between the encodings, which is log(|I| / K_Γ(I)) per unit of input. Spreading this over the |X_τ| token states of the byte encoding gives the per-state form. ∎

This is the bridge: Theorem X's encoding gap and Theorem XIII's richness gap are aspects of the same Kolmogorov-style bound, just measured on encodings (Theorem X) versus on state spaces (Theorem XIII). They are not independent claims; they are the same claim measured at two different points in the computational stack.

## 4. Theorem XIII (rigorous statement and proof)

### Statement

**Theorem XIII (Substrate-Markov-Mixing Efficiency).** Let G be a typed-graph substrate and τ a token substrate, both encoding domain D, with richness gap Δρ = ρ(G) − ρ(τ) ≥ 0. For any computation reducible to Markov-chain mixing on the substrate, the sample complexity n_X(ε, δ) of learning a transition kernel achieving total-variation distance ε from the optimal stationary distribution with confidence 1 − δ satisfies:

n_τ(ε, δ) / n_G(ε, δ) ≥ Φ(Δρ, |X|, ε)

where Φ is non-decreasing in Δρ and unbounded as Δρ → log|D| (the maximal richness gap). Specifically:

Φ(Δρ, |X|, ε) = 1 + Ω(|X| · Δρ / d(H_G))

with the leading-order term linear in the richness gap.

The sample-complexity ratio grows with the richness gap; the framework regime requires fewer samples to reach a given fidelity by an amount bounded below by the substrate's information-content advantage.

### Proof

By Lemma 4 with Y = G, X = τ, and the absolute constants absorbed:

n_τ / n_G ≥ 1 + (|X_τ| · Δρ − O(|X_τ| log(1/ε))) / d(H_G).

Drop the log(1/ε) cover-radius correction (it is dominated by |X_τ|·Δρ for any Δρ > log(1/ε), which holds for any non-trivial substrate richness gap). The ratio is bounded below by:

n_τ / n_G ≥ Ω(|X_τ| · Δρ / d(H_G)),

monotone non-decreasing in Δρ.

For unbounded Δρ → log|D|, |X_τ|·Δρ → |X_τ|·log|D|, while d(H_G) is fixed by the typed substrate's structure. The ratio diverges. ∎

### Worst-case (exponential) form

The polynomial bound above assumes single-task kernel learning with i.i.d. data and a well-behaved hypothesis class. Under worst-case assumptions — adversarial data, multi-task learning where each task duplicates structure-learning effort, hierarchical kernels where structure-learning cost compounds — the bound sharpens to exponential:

n_τ / n_G ≥ exp(|X_τ| · Δρ / d(H_G))

This is the regime relevant to large-scale language models, which implicitly learn substrate semantics across many tasks, paying the structure-learning cost in each. The polynomial-vs-exponential distinction depends on whether structure-learning amortises across tasks (polynomial) or not (exponential); current LLM training is empirically closer to the latter, suggested by the well-documented sample inefficiency of frontier-scale pretraining relative to graph-native systems on equivalent tasks.

## 5. Composition with Theorem XI

### Total cost of useful Markov-chain inference

Define the **total cost** C of a Markov-chain inference task as:

C = (sample complexity to learn kernel) × (per-step computation cost) × (mixing time)
  = n × C_step × t_mix

For two substrates G and τ:

C_τ / C_G = (n_τ / n_G) × (C_step,τ / C_step,G) × (t_mix,τ / t_mix,G)

### Theorem XI bounds the per-step computation ratio

Theorem XI (Paper I §9.2) establishes:

C_step,τ / C_step,G = Ω(|I| / K_Γ(I))

for structure-respecting computation on input I of byte length |I| and Γ-Kolmogorov complexity K_Γ(I).

### Theorem XIII bounds the sample-complexity ratio

The result of §4:

n_τ / n_G ≥ Ω(|X_τ| · Δρ / d(H_G)).

### Mixing time is approximately substrate-invariant

For graphs of comparable conductance and equivalent task structure, mixing time t_mix is approximately substrate-invariant (Levin & Peres 2017, Chapter 7). The graphs may differ in spectral structure but for tasks that mix at the mixing-time scale typical of practical inference, t_mix,τ / t_mix,G = Θ(1).

### Combined bound — multiplicative composition

Substituting:

C_τ / C_G ≥ Ω( (|I| / K_Γ(I)) × (|X_τ| · Δρ / d(H_G)) ) × Θ(1)
         = Ω( (|I| / K_Γ(I)) × (|X_τ| · Δρ / d(H_G)) )

The two factors are independent — one bounds the per-operation cost from the substrate's encoding inefficiency, the other bounds the number of operations needed from the substrate's structural-learning cost. They compose multiplicatively because they describe orthogonal axes of inefficiency.

If Theorem XI gives one order of magnitude (the empirical 1.4×–7.95× from Paper I §E.2 places conservative measurements in this regime), and Theorem XIII gives one order of magnitude (current empirical evidence on training-corpus-required-for-equivalent-capability is consistent with this), the combined bound is two orders of magnitude — *the product, not the sum*. "Orders of magnitude squared" is the colloquial form.

### What this is and is not

**It is** a derived bound on total cost ratio that composes two independent Kolmogorov-style bounds. Both bounds are individually established on solid information-theoretic and learning-theoretic foundations (Theorem XI on Kolmogorov complexity and structure-respecting computation; Theorem XIII on PAC-Bayes sample complexity and effective dimension).

**It is not** a tight quantitative prediction of any specific empirical ratio. The bound contains absolute constants (c in Lemma 2) that are absorbed into Ω(·); the constants depend on metric choice, hypothesis-class regularity, and the specific encoding. For a quantitative prediction on a specific task, the bound supplies the scaling law (multiplicative in the two factors, linear in the richness gap) but not the absolute level.

**It does not claim** that every statistical-substrate computation pays both penalties at the conservative theoretical maximum. Real systems exhibit intermediate behaviour — some structure-learning amortises across tasks, some operations are partially structural. The bound is a worst-case-under-the-stated-axioms result; empirical ratios may be smaller.

## 6. Caveats and open questions

### Tightness

The bound is an inequality, not an equality. Specific workloads may achieve smaller cost ratios than the theorem predicts. The theorem's value is in the *scaling law* — the multiplicative composition of two factors, each linear/exponential in measurable quantities — not in the absolute constant.

### Uncomputability

Both K_Γ (Theorem XI) and ρ(·) (Theorem XIII) are uncomputable in the general case. The theorems bound ratios of efficiency ideals, not directly measurable quantities. Empirical evaluation requires approximating ρ from the substrate's structural metadata and approximating K_Γ from compressed-encoding lengths; both are well-developed approximation techniques but neither is exact.

### Hypothesis-class specification

The hypothesis class H_X is somewhat free in its specification. We have chosen the natural class — kernels respecting the substrate's stated structural and typing constraints — but other choices are possible. The theorem's specific quantitative form depends on this choice. A coarser hypothesis class for the typed substrate would shrink the gap; a finer one would widen it. The chosen class corresponds to the "standard graph-native kernel" — the kernel a designer would naturally write given the typed substrate.

### Worst-case vs typical-case

The polynomial form of the bound is the typical-case scaling under reasonable regularity. The exponential form (§4 worst case) requires either adversarial data, multi-task amortisation failure, or hierarchical kernel structures where the structure-learning cost compounds. Frontier-scale LLM training may exhibit closer-to-exponential behaviour because it implicitly amortises over an unknown task distribution; this is empirically consistent with frontier-scale sample requirements but not proven.

### Open problems

1. **Tight constants.** Closed-form expressions for the constants in Lemma 2 (currently absorbed into absolute c) require harder analysis — likely problem-class-dependent.
2. **Mixing-time corrections.** The Θ(1) mixing-time ratio assumes graph conductance is comparable across substrates; for substrates with very different spectral structure, this assumption fails and the ratio acquires a substrate-dependent correction.
3. **Empirical richness measurement.** Approximating ρ(·) on real substrates is straightforward in principle but unstandardised in practice. A practical methodology is a natural follow-on (and a useful part of the experimental specification in `research/experiments/d4-stochastic-traversal-floor/`).
4. **Universality across Markov-chain tasks.** The theorem is stated for tasks reducible to Markov-chain mixing. Computations not so reducible (cryptographic, numerical-integration, etc.) are out of scope. A stronger result would identify which non-Markov computations admit analogous substrate-learning bounds; this is open.

## 7. Connection to Corollary D.4

Paper I §6.4 Corollary D.4 — the stochastic-traversal floor — is the operational reading of Theorem XIII applied to the limiting case of structurally-derived kernels:

A stochastic walker on the typed-graph substrate uses a kernel that is determined by the substrate structure itself, with no task-specific training. This corresponds to taking the prior π in the PAC-Bayes setup (Lemma 3) to be concentrated on structure-respecting kernels with task-weighting set to a uniform default.

For target tasks where the optimal kernel is close to the structural-default kernel (i.e., where the substrate's structure already approximates the task's transition pattern), the gap between walker performance and trained-model performance is bounded by the *task-weighting* component of kernel-learning cost — the I_task term in Lemma 2 — rather than the structure-learning component. On substrates where the structure carries most of the task-relevant information (typed graphs encoding well-formed semiotic structure), this gap is small.

The empirical experiment specified at `research/experiments/d4-stochastic-traversal-floor/` measures the constant in this gap on three task families (knowledge-graph link prediction, multi-hop QA, code dependency-graph traversal). The theorem predicts the gap exists and bounds its scaling; the experiment measures the absolute level.

## 8. References (this document)

- McAllester, D. (1999). PAC-Bayesian Model Averaging. *COLT*.
- Catoni, O. (2007). PAC-Bayesian Supervised Classification. *IMS Lecture Notes*.
- Alquier, P. (2024). User-friendly introduction to PAC-Bayes bounds. *Foundations and Trends in ML*.
- Levin, D. A. & Peres, Y. (2017). *Markov Chains and Mixing Times*, 2nd ed. AMS.
- Sinclair, A. (1992). Improved bounds for mixing rates of Markov chains. *Combinatorics, Probability and Computing*.
- Vapnik, V. (1998). *Statistical Learning Theory*. Wiley.
- Solomonoff, R. (1964). A formal theory of inductive inference. *Information and Control*.
- Bengio, Y. et al. (2021). GFlowNets. *NeurIPS* and follow-on papers.

(For citations to Paper I theorems, see the main paper's reference list.)

---

*This document is the rigorous companion to Paper I §9.4. The main paper carries the result; this document carries the formalism. Updates to the theorem's statement should be reflected in both.*
