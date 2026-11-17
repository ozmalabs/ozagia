# Ozma Serving Architecture — Implementation Plan

**Status:** Plan
**Scope:** What a serving layer built for the Ozma Protocol looks like —
architecture, components, and how it diverges from standard LLM serving.
**Companion to:** `protocol.md` (the wire protocol), `node-marketplace-security.md`
(the third-party-node security model).

---

## TL;DR

Standard LLM serving = stateless + prefix-cached + single-model + forward-streaming.

Ozma-native serving = **stateful-sessions + content-addressable-KV +
multi-model-pipeline + grammar-aware-streaming + tenant-isolated +
telemetry-integrated**.

Most of the building blocks exist (vLLM PagedAttention, xgrammar,
SGLang LoRA pools, Redis, Kafka). The protocol's infrastructure layer
is about assembling them with the right coordination, not inventing
novel primitives.

The seven items that determine whether you've built "LLM endpoint that
happens to speak Ozma" or "serving layer designed for the protocol":

1. **Chunk-level KV cache** indexed by content hash, not prefix.
2. **Multi-LoRA serving** with shared base model.
3. **Sticky session routing** keyed by session_id / workspace_id.
4. **Grammar-constrained generation** as a first-class endpoint.
5. **Decision-tree metadata** in every response.
6. **Per-tenant memory isolation** with encryption-at-rest for
   regulated deployments.
7. **Pre-warmed KV** for hot workspace chunks on session start.

Each is a 1–2 week project; together they are the difference.

---

## 1. Cache architecture

### 1.1 Content-addressable KV, not prefix-matched

Standard prompt caching (OpenAI, Anthropic) requires identical
prefix. The protocol's `{@HASH}` chunks hit cache regardless of order
— a session referencing chunks A, B, C in turn 3 and C, A, B in turn
5 gets both fully cached. Prefix caching misses the reorder.

**Implementation:** chunk-level KV cache indexed by content hash.
When a turn assembles context from three `{@HASH}` references, the
serving layer looks up pre-computed KV for each chunk and stitches
them together rather than recomputing from tokens.

This aligns with the paged-KV / chunked-prefill architectures
(vLLM PagedAttention, SGLang) rather than traditional sequence-as-a-unit
caching.

### 1.2 Cross-session warming

Redis-backed session cache with a 7-day TTL means a workspace's
canonical chunks (top outlines, hot functions, workspace memories)
recur across sessions. Predictive KV pre-compute becomes viable: when
a session starts against workspace W, warm the KV for W's top-K
chunks before the first turn arrives.

Standard serving has no hook for this. Implement a background worker
that subscribes to session-start events and triggers KV pre-compute
for the workspace's most-referenced chunks.

### 1.3 Minor-version KV invariance

Protocol v5.x minor revisions are backward-compatible at per-cap
grammar level. KV computed against v5.0 grammar remains valid against
v5.2. Cache does not need to invalidate on minor revisions — only on
major version bumps or GRAMMAR fingerprint changes to shared
primitives.

---

## 2. Session affinity

Standard LLM serving load-balances statelessly. The protocol
accumulates session cache, MEMORY, AGENT-STATE per session. A request
for session X should ideally land on the instance with that session's
state hot in RAM / GPU.

**Implementation:** sticky-session load balancing keyed by
session_id, with fallback rehydration from Redis if the target
instance is down. Consistent-hash routing with short TTLs outperforms
random or round-robin.

For MEMORY / AGENT-STATE (workspace-scoped), the scope key
(`workspace_id + goal_hash`) drives routing. Same workspace → same
instance. This keeps MEMORY injection fast because the vector index
is already loaded.

---

## 3. Multi-model pipeline

The request flow is classifier → tool_dispatch → tiny_model (maybe) →
local LoRA → upstream (maybe). Each stage is a different model:

| Stage | Model | Size | Notes |
|-------|-------|------|-------|
| Classifier | ONNX | ~10MB | Always loaded, CPU-OK, <1ms |
| Tiny model | GGUF | ~2GB | Parameter extraction; loaded once |
| Main LoRA | base + adapter | 60GB + 200MB | The workhorse |
| Upstream | API | — | Fallback for escalation |

### 3.1 Base model sharing across LoRAs

All v5 profiles (`ml-code-v5`, `api-code-v5`, `agent-v5`, `full-v5`)
share the Qwen 3.6 base. Serving keeps base weights in GPU memory once
and swaps the 200MB LoRA adapter per request. Use vLLM multi-LoRA or
SGLang LoRA pools.

### 3.2 Prefill vs. decode tuning

Standard serving batches prefill and decode together. Protocol
workloads have heavily compressed prefill (short inputs, many
sessions) and short decode (IR-compressed edits). This favors
aggressive batching on the decode side — more sessions per decoded
token.

Tune for small-input high-concurrency rather than the 4-16k input
batches standard serving assumes.

---

## 4. Constrained decoding as first-class

The grammar is PEG / ordered-choice. Not every inference engine
supports this. The serving layer should:

- Expose **grammar-constrained generation as a first-class endpoint**,
  not an afterthought. Registry flag `constrained_decode: true`
  dispatches to the constrained path.
- **Hot-load grammar per LoRA.** New LoRA version → new grammar
  fingerprint → new Lark / xgrammar / llguidance grammar object.
  Serving layer caches compiled grammars keyed by fingerprint.
- **Dual post-processing paths.** With constrained decoding → no
  parse failures → skip forgiving parsers. Without → fall through to
  the soft-fallback layer. Don't run both.

vLLM + xgrammar is the closest off-the-shelf path; SGLang has its own
grammar engine; llama.cpp's grammar support is weaker and older.

---

## 5. Structured streaming

Standard streaming is stateless: forward tokens as they arrive.
Protocol streaming is **buffered-with-checkpoints**:

- Ack header detection buffers first 3 non-blank lines or 512 bytes.
- `{@HASH}` expansion buffers until closing `}`.
- TRANSACTION blocks buffer until `end TN` arrives.
- MATH blocks buffer until first non-2-space-indent line.

**Implementation:** a grammar-aware stream processor on the response
side. Either the shim buffers on the way out (acceptable but adds
latency), or the inference engine surfaces structural events (better
but requires engine integration). vLLM with xgrammar constraints can
emit events at grammar-production boundaries — this is the aligned
architecture.

---

## 6. Pre-routing layer

Standard LLM serving is: request → model → response. The protocol's
pipeline has:

```
code_exec → tool_dispatch → tiny_model → classify → assemble → route → upstream
```

Most of these run outside the main model.

**Implementation:** expose the pipeline as a DAG with per-stage
budgets, not a single endpoint. Each stage has its own latency/cost
profile. `code_exec` and `tool_dispatch` are <$0 and <10ms; `upstream`
might be $0.20 and 8s.

Expose decision-tree metadata as a first-class response field (already
in the protocol via `X-Ozma-Decision` header and `_ozma.decision_tree`
body). The serving layer computes this per-request and includes it;
it changes what clients can build — auditable agents, cost-dashboard
UIs, cost-budget enforcement.

---

## 7. Per-tenant isolation

Standard serving does per-user rate limits. Protocol has per-user
MEMORY stores with actual data sensitivity. Workspace memories
contain code + context; user memories contain preferences.

**Implementation:**

- **Tenant-scoped vector indexes** for MEMORY retrieval (not global
  ChromaDB).
- **Tenant-scoped session caches** (Redis namespace per tenant).
- **DP budget tracking** per stable identity (OAuth `sub`), separate
  from the API-key rate limit.
- **Encrypted-at-rest memories** for regulated deployments, with key
  derivation per tenant.

This is closer to B2B SaaS architecture than to "LLM API" architecture.

---

## 8. Telemetry / training-data integration

Standard LLM serving emits access logs. The protocol emits structured
per-turn events with claim-verification outcomes, scope-check
results, confidence calibration.

**Implementation:** telemetry is dual-purpose — operational
observability AND training-data feedstock. The serving layer should:

- Emit structured telemetry events to Kafka / OTLP for ops.
- Fork opt-in events (`X-Ozma-Training: opt-in`) into the
  training-data pipeline with PII scrubbing.
- Apply the documented training opt-in filtering (drop
  `ack_hmac_fail`, `guardrail`, stream-interrupted turns, etc.) at
  ingestion.

---

## 9. Small operational wins

### 9.1 Prefill batching on small inputs

After compression, most turns have 500–2000 input tokens. Batching
32+ of these together for a single prefill pass is a throughput
multiplier. Standard serving is tuned for 4-16k input batches — tune
for small-input high-concurrency here.

### 9.2 Output pricing on external tokens, not internal

Users pay for what they see (expanded, uncompressed). The shim
renders cost based on external token count, not the IR-compressed
wire count. Expose both in telemetry so ops sees the compression
ratio.

### 9.3 Deterministic canary prompts for node-network attestation

If the third-party node marketplace ships (see
`node-marketplace-security.md`), the serving layer needs deterministic
inference mode with fixed seed for canary verification. Trivial to
add if the inference engine supports seeded generation (all do).

---

## 10. Component map

Concrete building blocks by responsibility:

| Responsibility | Component | Alternatives |
|----------------|-----------|--------------|
| Inference engine | vLLM with xgrammar | SGLang; llama.cpp (weaker grammar) |
| Multi-LoRA pool | vLLM LoRA adapters | SGLang LoRA pools |
| KV chunk cache | PagedAttention + content-hash index | Custom |
| Session state store | Redis (7-day TTL) | DynamoDB; Postgres |
| Vector index (MEMORY) | ChromaDB or pgvector (tenant-scoped) | Pinecone; Weaviate |
| Telemetry transport | OpenTelemetry / Kafka | Datadog; Honeycomb |
| Training-data pipeline | Kafka consumer → S3 → labeling | Custom |
| Load balancer (sticky) | Envoy consistent-hash | NGINX; Traefik |
| Classifier (ONNX) | onnxruntime (CPU) | TensorRT for GPU |
| Tiny model (GGUF) | llama.cpp server | Ollama |
| Auth / identity | OAuth2 (hosted); API key (BYOK) | — |

---

## 11. Implementation phases

### Phase 1 — Self-hosted OSS baseline (weeks 1–4)

Target: a single-node deployment that serves the protocol correctly
for single-user BYOK use.

- vLLM + xgrammar inference endpoint.
- Single-LoRA loading (load the `magic-lora` adapter at startup).
- In-memory session cache (no Redis yet).
- Local Chroma for MEMORY.
- OpenAI-compatible `/v1/chat/completions` endpoint.
- Decision-tree metadata in responses.

Outcome: `ozma` binary runs locally; protocol works end-to-end for one
user.

### Phase 2 — Multi-session + persistence (weeks 5–8)

Target: multi-session support with Redis-backed cache and cross-
session memory.

- Redis for session cache + MEMORY persistence.
- Sticky-session load balancing (Envoy or similar).
- Workspace scope identity from repo fingerprint.
- Tenant-scoped Chroma collections.
- Grammar-aware streaming buffer.

Outcome: multiple concurrent sessions, cross-session MEMORY,
cross-restart session recovery.

### Phase 3 — Multi-LoRA + pre-routing (weeks 9–12)

Target: full pipeline DAG with multiple model sizes and adapter
hot-swap.

- Multi-LoRA serving via vLLM LoRA pool.
- Classifier (ONNX) + tiny-model (GGUF) running in the pipeline.
- `code_exec` and `tool_dispatch` stages.
- Pipeline DAG orchestration with per-stage timeouts.
- Profile-aware routing (`ml-code-v5` → ML-LoRA, `api-code-v5` → API-LoRA).

Outcome: full request flow, most tasks never hit the main model.

### Phase 4 — Hosted-tier hardening (weeks 13–16)

Target: multi-tenant deployment with encryption, DP, auth, billing.

- OAuth2 identity (Clerk / WorkOS).
- Tenant isolation (Redis namespacing, Chroma collection per tenant).
- Encrypted-at-rest MEMORY for regulated tenants.
- DP budget tracking per OAuth `sub`.
- Stripe Connect integration (if pursuing node marketplace).
- Telemetry → Kafka → training-data pipeline.

Outcome: ozma.dev hosted tier ready for paying users.

### Phase 5 — Node marketplace (optional, later)

Target: third-party node operators earn from serving requests.

See `node-marketplace-security.md` for the security model.
Implementation builds on Phase 4 with additional primitives: node
sandbox, canary challenges, signed request/response envelopes, Stripe
Connect payouts.

---

## 12. Non-goals for v1

- **Fine-tuning from the serving layer.** Training-data capture yes;
  actual fine-tuning pipeline is a separate concern. Use the captured
  data offline.
- **Multi-region active-active.** Single-region first. Cross-region
  session migration is hard and unnecessary for v1.
- **Custom grammars per tenant.** The protocol grammar is fixed per
  version. Tenants can override protocol level (L1/L2/L3) but not
  redefine grammar.
- **Arbitrary model families as upstream.** Support OpenAI-compatible
  APIs (Anthropic, OpenAI, Mistral, DeepSeek) as the upstream
  fallback. Other APIs via adapters, not first-class.

---

## 13. Metrics to monitor

Beyond standard infrastructure metrics, Ozma-specific:

- **Chunk cache hit rate** (session cache + cross-session warm).
- **Compression ratio** (external / internal tokens) per request.
- **Decision-tree distribution** — what fraction of requests stop at
  `code_exec` vs `tool_dispatch` vs `local_lora` vs `upstream`.
- **Correction-loop depth** — turns per task completion.
- **Grammar-fingerprint mismatch rate** — indicates training/deploy
  version drift.
- **MEMORY injection latency** p50/p99.
- **Silent probe/ack latency** (when `strict_antispoof: true`).
- **Claim verification success rate** (FORMAL-CLAIM success / total).

---

## 14. Open questions for implementation

- **Cold-start latency** for multi-LoRA swap. Cold adapter-swap may
  add 100–500ms; hot pool of pre-loaded adapters helps but bounds
  tenant count.
- **Chunk eviction when Redis is at capacity.** Which chunks to keep
  warm across sessions? LRU at chunk-level is straightforward; a
  workspace-aware heuristic (keep top-K per workspace) may do better.
- **Streaming with constrained decoding** — some engines drop back to
  non-streaming when grammar constraints are on. Benchmark this with
  the target engine.
- **DP budget state consistency** across restarts. The training
  pipeline is the owner; the serving layer just emits markers — but
  resumption after a pipeline outage needs a replay story.
- **Canary prompts in production** — cost of running them as a
  fraction of real traffic. 1% sampling is probably fine; needs
  measurement.

---

## 15. The short version

Stand up vLLM with xgrammar and a single LoRA first. Add Redis and
sticky routing. Add multi-LoRA and classifier. Then harden for
hosted. Everything the protocol specifies has a standard off-the-shelf
component; the work is assembling them with the right coordination.

Most of the lift is in the **edges between components** —
content-hash-keyed KV cache, grammar-aware streaming buffer,
pipeline DAG orchestration, tenant-scoped state. The components
themselves are boring; the orchestration is where the protocol
earns its keep.
