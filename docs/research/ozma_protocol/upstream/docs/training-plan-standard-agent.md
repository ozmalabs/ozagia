# Standard Agent Training Plan

**Date:** 2026-04-19  
**Target profile:** `standard` (22 caps)  
**Protocol version:** v5.1  
**Estimated compute:** $124 production / Verda 4×H200 @ $13.56/hr / ~9h wall clock  

---

## Capabilities in scope

All v1.x caps plus v2.0 compression caps and v3.0 structured-conf/polyglot:

| Cap | Since | Purpose |
|-----|-------|---------|
| IR | v1.x | Greek-symbol code compression |
| EDIT | v1.x | Position-addressed code edits |
| OUTLINE | v1.x | Position-indexed code structure |
| RATIONALE | v1.x | Per-change explanation |
| WIRE | v1.x | Compressed prose |
| REASONING | v1.x | Structured thinking (hypothesis tree) |
| PLAN | v1.x | Multi-step execution plan |
| HINT | v1.x | Confidence-ranked localization |
| GRAPH | v1.x | Relational structure (call graphs) |
| TREE | v1.x | Hierarchical structure |
| CLAIM | v1.x | Natural-language verifiable invariants |
| TEST-SPEC | v1.x | Compressed test intent |
| INTERACT | v1.x | Multi-turn context requests |
| TRANSACTION | v1.x | Atomic edit groups |
| HANDOFF | v1.x | Cross-model/host context transfer |
| MEMORY | v1.x | Cross-session persistent knowledge |
| PERSONA | v1.x | Role and behavior configuration |
| LEARNED-BPE | v2.0 | Repo-specific identifier compression |
| ATTN | v2.0 | Attention-aware adaptive expansion |
| DOCSTUB | v2.0 | Docstring-specific compression |
| EMBED | v2.0 | Embedding references as session aliases |
| STRUCTURED-CONF | v3.0 | Per-aspect confidence maps |
| POLYGLOT | v3.0 | Cross-language identifier mapping |

**Not in scope:** FORMAL-CLAIM, COLLAB, FEDERATED, DP, GRAMMAR, MATH, TENSOR-SHAPE,
SCHEMA, DEP, EFFECTS, PROMPT, AGENT-STATE.

---

## Phase 1 — Infrastructure

### 1.1 Base model

**Base:** Qwen-3.6 (base model, not the L2 LoRA checkpoint)  
**Architecture:** 35B MoE (~3-4B active parameters per token)  
**Rationale:** Already the named target in the protocol registry (`qwen3.6-ozma-l3-v1`).
The existing L2 LoRA targets an earlier IR grammar revision; starting from that
checkpoint risks the old IR weights conflicting with the new training signal.
Cheaper and cleaner to train from the base model and teach IR alongside the
new caps in one pass.

**MoE training note:** The Ollama model is 24GB (Q4 quantized for inference).

**Primary: Verda 4×H200 @ $13.56/hr** (141GB each, 564GB total)  
Full bf16 LoRA: ~70GB weights across 4 GPUs = ~17.5GB per GPU. Memory
unconstrained — no QLoRA needed. H200's 4.8 TB/s HBM bandwidth suits MoE expert
weight loading better than H100 (3.35 TB/s). Estimated ~18,000 tok/sec.

**Alternative: 8×H100 SXM5 @ ~$19.92/hr** (80GB each, 640GB total)  
~20,000 tok/sec. Marginally faster wall clock; ~30% more expensive per run. Use
if Verda is unavailable.

### 1.2 LoRA configuration

```yaml
lora_r: 64
lora_alpha: 128          # 2× r is standard starting point
lora_dropout: 0.05
target_modules:
  - q_proj
  - k_proj
  - v_proj
  - o_proj
  # Exclude expert FFN layers (gate_proj, up_proj, down_proj inside MoE blocks):
  # protocol format learning is an attention-level behaviour; targeting experts
  # adds memory pressure and LoRA parameters without meaningful format benefit.
  # Revisit if structured output quality is poor after initial runs.
learning_rate: 2e-4
lr_scheduler: cosine
warmup_steps: 100
num_epochs: 3
batch_size: 8            # 8×H100 with 640GB total — memory unconstrained
gradient_accumulation_steps: 4   # effective batch = 32 (across 8 GPUs)
max_seq_length: 4096
bf16: true
gradient_checkpointing: true
```

Verda 4×H200 (primary). See MoE training note above.

### 1.3 Tooling

- **Training:** `axolotl` (simplest config YAML surface) or `LLaMA-Factory`
- **Grammar validation CI:** existing script scanning `protocol.md` §Formal Grammar
- **Format compliance eval:** PEG parser from the grammar (Lark in Earley mode)
- **HMAC probe/ack test:** simple shim-side integration test

---

## Phase 2 — Training data generation

**Target:** ~49,000 examples, ~39M tokens, covering training example types 1–14
from §Training Data Format.

### 2.1 Generation strategy

All data is synthetically generated using a frontier model (Opus 4.7 or equivalent)
with the protocol spec as the generation prompt. Each example type gets a generator
prompt that enforces the grammar and produces diverse code scenarios.

Do **not** use a single code domain. Cover: Python, TypeScript, Go, Rust, Java,
SQL — weighted by prevalence. Cover: bug fixes, feature adds, refactors,
documentation, test writing, dependency analysis.

### 2.2 Example budget by capability group

| Group | Example types | Target count | Avg tokens | Total tokens |
|-------|--------------|--------------|------------|--------------|
| Core edits (EDIT, OUTLINE, RATIONALE, WIRE) | 1, 4 | 12,000 | 600 | 7.2M |
| IR compression | 2 (IR variant) | 4,000 | 550 | 2.2M |
| Session cache | 8 | 3,000 | 700 | 2.1M |
| Reasoning + Plan | 7 (reasoning variant) | 4,000 | 850 | 3.4M |
| Hint + Graph + Tree | 5, 6 | 4,500 | 650 | 2.9M |
| Claim + Test-spec | 7 | 5,000 | 800 | 4.0M |
| Interact + Transaction | 3 | 4,000 | 900 | 3.6M |
| Memory + Persona | 9, 10 | 5,000 | 950 | 4.75M |
| Handoff | (handoff variant) | 3,000 | 1,200 | 3.6M |
| BPE + Embed + Docstub + Attn | 11, 12 | 5,000 | 800 | 4.0M |
| Structured-conf | 13 (conf variant) | 3,000 | 750 | 2.25M |
| Polyglot | 14 | 2,500 | 900 | 2.25M |
| **Total** | | **~55,000** | | **~42M** |

Slight overrun from the cost estimate is intentional — better to have more examples
and subsample than to under-train on any single capability.

### 2.3 Critical invariants in generated data

Every generated example must satisfy:

1. **Ack appears exactly once** — first assistant turn only. Correction and subsequent
   turns never re-emit.
2. **Grammar compliance** — all structured output parses against §Formal Grammar.
   Run the Lark parser on every assistant turn before accepting the example.
3. **Position addressing** — `§N` not `[N]`. Stable IDs in outlines.
4. **No capability bleeding** — examples for EDIT-only scenarios must not emit CLAIM
   blocks unless the CLAIM cap is being exercised. Cap boundaries must be clean.
5. **Ack fingerprints match the v5.1 registry** — generated acks must use the correct
   fingerprints from the capability registry in protocol.md.
6. **HMAC plausibility** — generated acks use a valid 8-hex HMAC field (can be a
   placeholder during training; shim verifies separately).

### 2.4 Negative examples

Include ~10% negative examples — cases where the model should NOT emit a structured
block:

- Turns after the first (no ack)
- Prose-only responses (WIRE only, no EDIT)
- L1 fallback scenarios (model instructed to degrade gracefully)

These teach the model what NOT to produce, which is as important as what to produce.

### 2.5 Validation pipeline

```
generate → grammar_check → cap_boundary_check → dedup → split (90/5/5 train/val/test)
```

Reject rate target: <5%. If above 10%, the generator prompt needs revision.

---

## Phase 3 — Training

### 3.1 Hyperparameter search

Run 10 short jobs at 30% of training data (15k examples):

```
learning_rate: [1e-4, 2e-4, 3e-4]
lora_r:        [32, 64, 128]
warmup_steps:  [50, 100, 200]
```

Primary metric: **grammar compliance rate on validation set** (not loss).  
Secondary metric: **ack format precision** (ack appears exactly once, correct position).

Use the format compliance eval (Lark parser) as the sweep metric, not perplexity.
Perplexity doesn't tell you whether structured output is parseable.

### 3.2 Full training runs

Run 3 full training jobs with the best 3 hyperparameter configurations.
Select the checkpoint with highest grammar compliance on the held-out test set.

Expected timeline (Verda 4×H200 @ $13.56/hr, ~25,600 tok/sec total):
- Each hyperparam run: ~23 minutes (30% data × 1.3h full)
- 10 hyperparam runs: ~4 hours wall time
- Each full run: ~1.3 hours wall time
- 3 full runs: ~4 hours
- Eval: ~0.5 hours
- **Total: ~9 hours wall time / $124**

### 3.3 Checkpoint selection

Eval each checkpoint at: epoch 1, epoch 2, epoch 3.
Select the epoch with highest grammar compliance — not necessarily the final epoch
(overfitting to format is real; the model starts repeating training examples verbatim).

---

## Phase 4 — Evaluation

### 4.1 Format compliance (automated)

For every test-set example, parse the assistant turn against §Formal Grammar using
the Lark parser. Report:

- **Overall grammar compliance rate** — target: >97%
- **Per-capability compliance rate** — identify which caps are weakest
- **Ack format precision** — ack appears in position 1, correct fingerprints, 8-hex HMAC
- **No-ack compliance** — subsequent turns correctly omit the ack header

### 4.2 Shim integration tests

Run the trained model against the actual shim (not just grammar eval):

- Probe → ack exchange: verify HMAC validates
- Session cache: verify `{@HASH}` references are emitted and handled
- Scope check: verify edits address the right positions
- CLAIM verification: verify CLAIM output is plausible (heuristic check)
- MEMORY roundtrip: `remember:` in turn N, verify recall in turn N+1

### 4.3 Code quality eval

Sample 200 edit tasks from SWE-bench-lite or a held-out real-repo set.
Compare:
- Baseline (same 7B model, no LoRA, L1 protocol)
- Trained model (LoRA, L3 protocol)

Metric: patch correctness rate (does the edit compile + pass existing tests).
Expected lift: 10-25% from better context utilization (INTERACT, session cache,
RATIONALE-guided edits).

### 4.4 Regression check

Verify the LoRA does not degrade the base model's general coding ability on
HumanEval / MBPP. A well-configured LoRA should be near-neutral here (<2% drop).
If >5% drop, the training data is too narrow or learning rate is too high.

---

## Phase 5 — Registry and deployment

### 5.1 Fingerprint generation

For each cap in the trained model's registry entry, compute:
```python
sha256(grammar_section_for_cap)[:6]
```
using the §Formal Grammar section of protocol.md v5.1.

These must match the fingerprints the model was trained to ack. If there is any
discrepancy (grammar was edited after training data was generated), the ack will
fail the shim's fingerprint check and the model will fall back to L1.

### 5.2 Registry entry

```python
MODEL_CAPS = {
    "qwen3.6-ozma-standard-v1": {
        "level": 3,
        "profile": "standard",
        "caps": {
            "IR":               "ab12cd",
            "EDIT":             "b2c1d3",
            "OUTLINE":          "a4f9c1",
            "RATIONALE":        "e1a7f4",
            "WIRE":             "...",
            "REASONING":        "71ab3c",
            "PLAN":             "82bc4d",
            "HINT":             "f9b2a6",
            "GRAPH":            "3c4d1e",
            "TREE":             "5e6f2a",
            "CLAIM":            "c3d4b5",
            "TEST-SPEC":        "93cd5e",
            "INTERACT":         "d8f3e2",
            "TRANSACTION":      "...",
            "HANDOFF":          "04de6f",
            "MEMORY":           "cd34ef",
            "PERSONA":          "ef5601",
            "LEARNED-BPE":      "1a2b3c",
            "ATTN":             "9d8e7f",
            "DOCSTUB":          "6c5b4a",
            "EMBED":            "3f2e1d",
            "STRUCTURED-CONF":  "8a7b6c",
            "POLYGLOT":         "2e1f0a",
        },
        "ir_tokenizer": "qwen",
        "allow_persona_override": true,
        "strict_antispoof": false,
        "dp_user_id": null,
        "constrained_decode": false,
    }
}
```

Fingerprints marked `"..."` need to be computed from the current grammar once the
training data generation grammar snapshot is locked. **Lock the grammar before
generating training data and do not edit §Formal Grammar between data generation
and deployment.**

### 5.3 Secret registration

The HMAC uses `sha256(nonce + model_secret)[:8]`. Register `model_secret` in the
shim's secret store (not in the registry file). The model is trained to produce
this HMAC; the secret must match what the shim expects.

Practical approach for training: use a fixed placeholder secret during data
generation, then bake the real secret into the final training run or fine-tune
a single additional "secret injection" step. The HMAC is a training invariant —
the model learns to produce it, so the secret used during data generation must
match the secret registered in the shim.

---

## Open questions before starting

1. **Grammar snapshot lock date** — all training data must be generated against the
   same grammar version. v5.1 is the current version. Confirm no protocol edits are
   planned before data generation completes.

2. **HMAC secret management** — decide on secret injection approach (training-time
   bake-in vs. post-hoc fine-tune) before data generation starts.

4. **Eval corpus** — SWE-bench-lite is public and appropriate. Confirm whether a
   proprietary real-repo eval set exists or needs to be built.

5. **Telemetry loop** — plan for how production telemetry feeds back into v2 training
   data. The `X-Ozma-Training: opt-in` header exists; decide which deployments will
   opt in and how the telemetry pipeline filters and formats examples.

---

## Risks

| Risk | Likelihood | Mitigation |
|------|-----------|------------|
| Grammar compliance < 95% | Medium | More negative examples; higher LoRA rank |
| Ack-after-turn-1 regression | Medium | Explicit negative examples for turns 2+ |
| Base model capability regression | Low | Eval on HumanEval before/after; stop if >5% drop |
| Fingerprint mismatch at deployment | Low | Lock grammar before data gen; CI check |
| HMAC secret mismatch | Low | Integration test probe→ack exchange pre-deployment |
| Training data format errors | Medium | Grammar validator in generation pipeline; reject >5% rate |
