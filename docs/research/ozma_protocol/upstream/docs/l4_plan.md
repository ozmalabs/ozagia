# Ozma Protocol L4 — Structured Protocol, Arbitrary Encoding

**Status:** spec landed (chars + tokens encodings enumerated), PoC pending
**Protocol version:** v6.0 (unchanged from v5.x on the grammar axis)
**Target PoC:** transparent shim-side compression for `ozma-coder-3b-lora`
and `ozma-wizard-1-l4`, measured as 3-4× cost + wallclock reduction with
zero client-visible change.

## Thesis

**The Ozma protocol is structure. The wire is encoding.** L3 and earlier
conflated the two — `[OZMA v5.2 probe:abc123 caps: IR:ab12cd]` was
simultaneously the protocol grammar and its serialization. L4 separates
them. The grammar (probe / ack / caps / wire / edit / plan / claim /
reasoning / remember / test-spec / schedule / motion / ...) is invariant.
The *encoding* of that grammar on a particular wire is negotiable and
model-specific.

Three named encodings ship in v6.0:
- **chars** (default, human-readable): the literal bracketed strings v5.x
  used. This is the client-facing encoding and the inspection ground truth.
- **tokens** (L4a): the same grammar rendered into a model-native BPE
  vocabulary. Every structural primitive maps to a short token sequence —
  often a single token — that the model's weights recognize natively.
- **activations** (L4b, deferred): grammar rendered directly into
  embedding vectors / KV cache entries. Research-grade; spec reserves
  the encoding name but does not implement it in the PoC.

A **dialect** is a specific realization of the tokens encoding for a
specific model: the tokenizer's vocab + merges + special tokens + the
mapping from protocol primitives to token IDs. Dialects are static
artifacts with stable fingerprints; ~200KB for a 16-32k BPE. They ship
with the model.

## What doesn't change

Everything above the codec. The parser operates on the decoded structural
form; downstream consumers (verifier, telemetry, stats, dev loop, edit
applier) see AckHeader / EditCmd / PlanBlock / ClaimBlock / etc. exactly
as they do in L3, regardless of which encoding was on the wire. A probe
decoded from chars and a probe decoded from tokens are byte-identical
Python objects.

- Grammar primitives: unchanged from v5.x
- `TurnParser`, `HandshakeManager`, `OzmaShim.intercept_*`: unchanged
  public surface
- Capability fingerprints, HMAC, handshake state machine: unchanged
- Client-facing API (`/v1/chat/completions`, OpenAI-compatible): unchanged

## What does change

Below the codec. A new transcoding layer sits between the shim and the
model backend, active iff the model's `wire_format` is non-default.

- `WireFormat` literal: `"chars" | "tokens" | "activations"`
- `ModelEntry.wire_format` field (default `"chars"`)
- `ModelEntry.dialect_fingerprint: str | None` — the `.ozmap` that this
  model's weights were trained on, None for chars-only models
- `DialectStore` — maps fingerprint → loaded codec (BPE vocab + merges +
  protocol-primitive mapping table)
- `OzmaShim._encode_for_backend(turn_text, entry)` — applied on egress
  when `entry.wire_format == "tokens"`, emits token IDs via the dialect
- `OzmaShim._decode_from_backend(token_ids, entry)` — applied on ingress
  before the existing parser runs

The backend transport shifts from `messages: [...]` to `prompt_token_ids:
[...]` on the vLLM endpoint — a supported shape on `/v1/completions`
today, already routable.

## Client-transparent by construction

The client talks to the shim in chars (the OpenAI-compatible API it was
always using). The shim encodes into the model's dialect on the way to
the backend, decodes on the way back, and returns chars to the client.
Nothing above the shim changes.

```
Client   ── plaintext /v1/chat/completions ────►   OzmaShim
                                                     │
                                            [encode via dialect]
                                                     ▼
                                                   Model (vLLM)
                                                     │
                                            [decode via dialect]
                                                     ▼
         ◄── plaintext /v1/chat/completions ──    OzmaShim
```

The client's `usage.input_tokens` and `usage.output_tokens` reflect the
*backend* token counts — the compressed ones. So the client sees plaintext
in and out, but pays for compressed tokens. Cheaper, faster, longer
effective context, no SDK change.

This is the gzip-over-HTTP pattern applied to LLM APIs. The compression
boundary is the shim; above it, everyone is blissfully unaware.

## Inter-shim / inter-model comms (deferred)

When two Ozma shims talk to each other directly (agent-to-agent over
OzmaNet), they can skip the chars transcode and exchange tokens directly
— as long as they share a dialect. The handshake in that case carries
the dialect fingerprint, and on fingerprint miss the emitter can push
its `.ozmap` once (cached by the receiver, keyed by fingerprint). This
is the "dialect push" mechanism sketched in earlier design notes.

**Deferred for the PoC.** The client-transparent server-side compression
path is independently valuable and ships without any peer coordination.
Inter-shim negotiation is a natural follow-on — the spec reserves
`map:<type>:<fp>` and `map-push` / `map-ack` frame types, but they stay
unimplemented until there's a concrete pair of shims that need them.

## Falsifiable claim

A coder model served through an L4-aware Ozma shim, with a dialect
trained on IR-compressed code + protocol traces, delivers **≥ 3× token
reduction** on HumanEval-style code-edit traffic relative to the same
base model served via L3 chars, at pass@1 within -3 points of baseline.

- N < 2: dialect choice is wrong, retry with different corpus mix.
- N ≥ 3 but pass@1 drops > 5 points: dialect-to-weights alignment is
  weak, retry with more epochs or better init.
- Neither kills the thesis.

Secondary metrics:
- Wallclock per task (expected: ~linear with tokens, so ~1/N)
- Cost per task (linear in tokens, so ~1/N)
- Parse reliability (target: ≥ 99% on structurally-valid turns — the
  decoder should never emit ill-formed AST)
- Round-trip fidelity: encode(decode(x)) == x for every AST node type.
  This is the codec correctness invariant and is tested independently
  of any trained model.

## Track A — L4a-fast (ship now, ~1 week)

Goal: prove the client-transparent compression pipeline end-to-end on
a real model. Empirical signal in hand before committing to the fuller
custom-BPE + finetune on Track B.

**Dialect:** LoRA + extended tokenizer over Qwen 2.5 3B.

1. Extend the base tokenizer with Ozma IR glyphs + protocol keywords
   (see `models/ozma-coder-3b-lora/scripts/extend_tokenizer.py`, already
   landed). ~80 new token IDs. Serialize the mapping from protocol
   primitives to token IDs as the dialect's `.ozmap`.
2. `model.resize_token_embeddings(len(tokenizer))`, LoRA with
   `target_modules = ["q_proj","k_proj","v_proj","o_proj","embed_tokens","lm_head"]`.
   Only the new embedding rows learn; base rows stay effectively frozen.
3. Train on ~100k synthesized IR + protocol examples (1-2 H100-hours).
   Dataset: run `to_ir(...)` over existing Python training data, wrap in
   synthetic probe/ack/edit/plan/claim envelopes.
4. Serve via vLLM with the extended tokenizer (`--tokenizer` override).
   Register the model in `MODEL_CAPS` with `level=LEVEL.L4,
   wire_format="tokens", dialect_fingerprint=<fp>`.
5. Wire the shim's `_encode_for_backend` / `_decode_from_backend` around
   the existing `intercept_request` / `intercept_response` methods.

**What Track A validates:**
- Codec correctness: encode/decode is lossless on every AST node type.
- Client-transparent pipeline: chars in at `/v1/chat/completions`,
  chars out, backend traffic in tokens.
- Structural round-trip: a trained model emits dialect-encoded protocol
  blocks that decode to well-formed AST nodes ≥ 99% of the time.

**What Track A does *not* validate:**
- The compute-efficiency claim. Base model's attention still operates
  over native-language priors; new token rows are projections onto
  existing latents. Expect output-token savings (fewer tokens per line)
  but only a modest forward-pass reduction — the LoRA can't reshape the
  base's notion of "what code is."

**Acceptance for Track A:**
- Codec lossless on a 500-turn fixture covering every AST type
- Pass@1 ≥ 95% on a 200-task internal dev set vs baseline plaintext
- Token-per-task reduction ≥ 1.5× (the LoRA ceiling; 3× is Track B)
- Full A/B bench via existing `tests/bench_kimi_k26_ab.py` machinery

## Track B — L4a-full (ship after A signals, ~3 weeks)

Goal: deliver the compute-efficiency claim by making the dialect the
model's native prior, not a LoRA-bolted overlay.

**Dialect:** custom BPE trained on IR-rendered code + protocol traces.

1. Train BPE on a ~10M-line IR + protocol corpus via HuggingFace
   `tokenizers`. Vocab 16-24k; measure compression ratio vs cl100k on a
   held-out set before committing. Expected win: 3-4× on IR code, 5-7×
   on dense protocol blocks, maybe 1.5-2× on natural-language prose.
2. Full finetune (not LoRA) of a 3B base. Dialect *is* the model's native
   token distribution; compressed form *is* its prior. Fewer forward
   passes per logical operation is the load-bearing claim.
3. Same serve + bench + integration as Track A.

**What Track B validates:**
- Compute-efficiency. If the token reduction doesn't translate to
  wallclock/cost reduction in the bench, the dialect is wrong somewhere
  (entropy distribution mismatch, merge list too aggressive, etc.).
- Ceiling of the shim-side compression pattern. Above this is L4b
  (activations / latent KV handoff) which is a different serving stack.

**Acceptance for Track B:**
- N ≥ 3 token reduction vs chars baseline, N ≥ 2 vs Track A
- pass@1 within -3 points of baseline
- Shim-side compression doesn't bottleneck: encode + decode wallclock
  < 5% of end-to-end request latency

## Files touched

### Spec + encoding layer (landed / in-flight)

- [x] `ozmalib/protocol/registry.py` — `LEVEL.L4`, `WIRE_FORMATS`, `ModelEntry.wire_format`
- [x] `ozmalib/protocol/handshake.py` — v6.0, `wire:<format>` in probe/ack
- [x] `ozmalib/protocol/__init__.py` — export `WIRE_FORMATS`
- [x] `tests/test_wire_format.py` — 23 spec tests green
- [ ] `ozmalib/protocol/registry.py` — `ModelEntry.dialect_fingerprint`
- [ ] `ozmalib/protocol/dialect.py` (new) — `DialectStore`, `DialectCodec`,
      `load_dialect(path) -> DialectCodec`, `encode(ast) -> list[int]`,
      `decode(token_ids) -> ast`
- [ ] `ozmalib/protocol/shim.py` — `_encode_for_backend` / `_decode_from_backend`,
      gated on `entry.wire_format == "tokens"`
- [ ] `tests/test_dialect_codec.py` — round-trip fidelity, every AST node type

### Track A — fast-path dialect

- [x] `models/ozma-coder-3b-lora/scripts/extend_tokenizer.py`
- [x] `tests/test_l4_tokenizer_extension.py`
- [ ] `models/ozma-coder-3b-lora/scripts/build_dataset.py` — synth IR+protocol corpus
- [ ] `models/ozma-coder-3b-lora/config/lora_l4.yaml` — `target_modules` includes embed + head
- [ ] `models/ozma-coder-3b-lora/scripts/serve_vllm.sh`
- [ ] `models/ozma-coder-3b-lora/dialect.ozmap` — serialized codec artifact
- [ ] `ozmalib/protocol/registry.py` — register `ozma-coder-3b-l4-lora` entry

### Track B — full-finetune dialect

- [ ] `models/ozma-coder-3b/scripts/train_bpe.py` — corpus-trained BPE
- [ ] `models/ozma-coder-3b/scripts/build_corpus.py`
- [ ] `models/ozma-coder-3b/config/axolotl_l4.yaml`
- [ ] `models/ozma-coder-3b/dialect.ozmap`

### Bench + observability

- [ ] `tests/bench_l4_vs_baseline.py` — end-to-end A/B via existing telemetry
- [ ] `ozmalib/cli/stats.py` — `wire_format` and `dialect_fingerprint`
      columns in `recent`

## Deferred (named but not in PoC)

- **Inter-shim dialect push.** `map:<type>:<fp>` field in probe, `map-push`
  / `map-ack` frames, cached `DialectStore` keyed by fingerprint.
  Implemented when the first shim-to-shim use case lands.
- **L4b / activation-native wire.** Reserved `wire_format="activations"`.
  Needs architecture cooperation (shared KV cache, residual-stream
  handoff) and a custom serving runtime. Revisit once L4a validates.
- **Multi-dialect models.** A model that understands several dialects at
  serve time — useful for inter-shim traffic where the peer's dialect is
  known. Trivial extension of `DialectStore`, but not load-bearing for PoC.
- **Signed dialect push.** Once inter-shim push ships, the `.ozmap` is an
  untrusted artifact crossing trust boundaries. Needs signing + allowlist.
  Pure BPE dialects are data (low-risk); gist-token / learned-codec
  dialects are model weights (supply-chain concern).
- **Multi-language IR corpus.** Python-only for PoC. JS/Go/Rust follow
  the same pattern.

## Risk flags

1. **Codec round-trip fidelity.** The structural invariant is "encode ∘
   decode = id on every AST node." If the tokenizer loses a protocol
   glyph or a grammar primitive on one path, the whole spec is broken.
   Mitigation: a per-AST-type round-trip test, already in the plan.
2. **Dialect-to-weights alignment.** A LoRA over a base tokenizer can
   learn to *emit* dialect tokens but may not align them to the
   semantics the base model already has for the underlying concepts.
   Mitigation: start with a 200-task parse-reliability gate before
   investing in bench-level pass@1 measurement.
3. **Streaming decode.** The shim has to decompress on the fly.
   BPE decode is stateless per-token, so this is trivial for Track A/B.
   For L4b it becomes a research problem — deferred.
4. **Backend token-ID support.** vLLM's `/v1/completions` with
   `prompt_token_ids` is the supported backend path. Other backends
   (llama.cpp server, hosted providers) may not expose pre-tokenized
   input, which means the shim falls back to chars for those.
   Acceptable: L4 is an internal optimization, not a portability
   requirement. Each backend opts in.

## Acceptance gate for L4 → mainline

PoC is shippable when:
- Codec is lossless on every AST node type
- Track A ≥ 1.5× token reduction + pass@1 within -3 points
- Client `/v1/chat/completions` surface is byte-identical to pre-L4
  (regression suite passes unchanged)
- `ozma-stats` surfaces `wire_format` and `dialect_fingerprint` per call

Below that bar, L4 stays in the research branch. The spec stays in
mainline either way — it's cheap, it's correct, and it establishes the
encoding layer for any future dialect experiment.
