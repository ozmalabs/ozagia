# Protocol Training Cost Analysis

**Date:** 2026-04-19  
**Protocol version:** v5.1  
**Compute basis:** H200 at $3.99/hr

---

## Methodology

LoRA fine-tuning only — not full pretraining (~1/1000th the compute. Training data
generation cost is separate and not included. "Production run" = 3 full runs + 10
hyperparameter sweeps at 30% data + 20% eval overhead ≈ 7.2× a single run.

Throughput assumptions: 7B/1×H200 = 18k tok/s · 13B/2×H200 = 22k tok/s · 30B/4×H200 = 16k tok/s. 3 training epochs throughout.

---

## Options

Base model: **Qwen-3.6 (35B MoE, ~3-4B active params/token)**  
Ollama inference model is 24GB (Q4). Full bf16 LoRA throughout.

**Primary: Verda 4×H200 @ $13.56/hr** (564GB total)  
35B MoE: ~8k tok/sec per H200; 4 GPUs × 0.80 scaling efficiency = **~25,600 tok/sec total**.
H200's 4.8 TB/s bandwidth suits MoE expert weight loading well.

**Alternative: 8×H100 @ ~$19.92/hr** (640GB total)  
8 GPUs × ~6k tok/sec × 0.75 efficiency = **~36,000 tok/sec total**. Faster wall
clock but ~60% more expensive per run.

Note: original $52 estimate assumed a 7B model (18k tok/sec/GPU). 35B MoE is
~8k tok/sec/GPU — the ~2× cost increase is entirely the model size, not GPU count.

| Option | Caps | Passes (3×) | 4×H200 wall/run | 4×H200 single run | 4×H200 production (×7.2) | 8×H100 production |
|--------|------|-------------|-----------------|-------------------|--------------------------|-------------------|
| Wire Core | 12 | 46M | 0.5h | $7 | $49 | $69 |
| **Standard Agent** | **22** | **117M** | **1.3h** | **$17** | **$124** | **$174** |
| ml-code-v5 | 25 | 141M | 1.5h | $20 | $147 | $206 |
| api-code-v5 | 26 | 145M | 1.6h | $21 | $151 | $212 |
| Full − FORMAL-CLAIM | 33 | 207M | 2.2h | $30 | $216 | $303 |
| Full v5.1 | 34 | 246M | 2.7h | $36 | $257 | $360 |

_4×H200: wall time × $13.56. 8×H100: wall time × $19.92. Production = ×7.2._

---

## Capability breakdown: Standard Agent vs api-code-v5

api-code-v5 adds four caps over Standard Agent: **SCHEMA, DEP, EFFECTS, PROMPT**.

None of the four additions improve benchmark scores (HumanEval, MBPP, SWE-bench,
LiveCodeBench). They are shim-verifiable improvements, not model-reasoning
improvements. The model can make schema changes, dep upgrades, and write prompt
strings equally well without the structured output type — it just does it as a
regular EDIT.

### What each cap actually adds operationally

**SCHEMA** — shim gains a breaking-change gate. The model declares `[compatible]`
or `[breaking]`; the shim cross-checks the actual diff. `schema_compat_mismatch`
fires when the declaration and diff disagree (the dangerous case: model thinks it's
compatible, shim detects it isn't). `[breaking]` changes can be held for operator
approval.

**DEP** — shim gains CVE resolution verification. The shim checks a CVE database
and emits `dep_cve_unresolved` if the upgraded package version doesn't close the
named CVE. Structural audit trail of dep operations vs. diff-only history.

**EFFECTS** — shim gains side-effect contracts. `pure=true` contradictions caught
at output time. Test runners know which functions need setup/teardown vs. which are
safe to call bare. Sandboxed deployments can gate on `network=external` automatically.

**PROMPT** — shim gains injection-resistance scanning and token-budget monitoring
for prompt strings in LLM-app code. `prompt_injection_risk` fires at edit time
rather than at security review.

### Verdict

api-code-v5 is worth the ~$50 extra training cost only if those specific enforcement
gates are real operational needs: API teams shipping microservices where breaking
changes matter, security-sensitive deployments tracking CVE resolution, sandboxed
code execution, or LLM-app development. For a general-purpose coding assistant,
Standard Agent handles the underlying code work equally well.

---

## Key findings

**Cost is dominated by GPU count, not data volume.** The jump from Standard Agent
($52) to Full−FC ($414) is 8×, but ~4× of that is going from 1 GPU to 4 (30B
model). Full−FC on a 7B model would cost ~$100 production.

**FORMAL-CLAIM's training compute delta is small ($78).** The real cost is data
generation: valid SMT-LIB/Dafny/Lean 4 examples require formal methods expertise,
running actual verifiers to label each example, and correction-loop data for the
refuted-claim feedback cycle. Bad formal specs corrupt the training signal with no
graceful degradation.

**Domain split beats monolithic for mid-tier.** Two separate 13B models (ml-code +
api-code, ~$210 combined) give better per-domain quality than one 30B model at
similar cost.

**Standard Agent at $52 covers the vast majority of real deployments.** Incremental
cap addition is the primary upgrade path — one new registry entry + one ack
fingerprint per cap added.

---

## Recommended sequence

```
1. Standard Agent  ($124)  → Verda 4×H200, ~9h wall clock, ships useful product
2. api-code-v5     ($151)  → only if SCHEMA/DEP/EFFECTS/PROMPT gaps confirmed
3. ml-code-v5      ($147)  → only if ML-code use cases confirmed in telemetry
4. Full−FC         ($216)  → only if FEDERATED/DP/AGENT-STATE are actually needed
5. Skip Full v5.1          → FORMAL-CLAIM data generation quality is the gating
                              cost, not compute ($41 delta in training compute)
```
