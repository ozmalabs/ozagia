# Ozma Protocol Review

**Reviewer:** Claude (Opus 4.7)
**Date:** 2026-04-18
**Target:** `protocol.md` v1 Draft
**Cross-checked against:** `ozmalib/context/ir_shim.py`, `ozmalib/compress/edit_protocol.py`, `ozmalib/compress/interaction.py`

The protocol is well-structured. The issues below are places where the design will bite under real workloads.

---

## High-impact issues

### 1. The handshake is behavior-coupled and spoofable

Relying on the model to echo `[OZMA v1 ack: ...]` as its first non-whitespace output in 64 chars is brittle:

- **Thinking/reasoning models** (Claude 4.5+, R1-style) emit internal tokens or whitespace first. A leading newline breaks the 64-char window.
- **RLHF tax:** stock models that *would* follow the instruction get penalized by "Sure!" or a markdown heading prepended.
- **False positives:** a stock model faithfully parroting the system prompt can emit the ack line and spoof an upgrade to L3, triggering wire expansion on prose that isn't wire-encoded.
- **Security:** nothing prevents user-controlled content from injecting an ack header.

**Fix:** make capability negotiation out-of-band. You know which LoRA is loaded — use a model registry / config key (`model_caps["qwen3.6-ozma-l3"] = {...}`). Keep the in-band ack as a *sanity check* with a challenge-response (shim sends a nonce; model echoes `ack <hash>`), not the source of truth. Protocol.md §Handshake should document that the in-band path is advisory.

### 2. Positions are not stable across turns

`[6]` is "implicit 1-based document order". Any `add [3]:before` renumbers everything below, silently invalidating every `[N]` the model remembers from earlier turns and any pending `expand [N]` command. The correction loop under Scope Check can land on the wrong node.

**Fix:** assign stable IDs (first 8 chars of `sha1(qualified_symbol + signature)`) in addition to ordinal position. Show both to the model (`[6 = 3f9a1c]`), accept either in edits, store the hash in the edit record. When the outline re-renders after a patch, the model's in-memory `3f9a1c` still resolves — even if its ordinal is now `[4]`. This also lets the shim validate "model addressed position X — does the current outline still contain that ID?" without guessing.

### 3. `[N]` markers collide with content

Python array indexing, regex character classes, DOT/Mermaid labels, citation markers `[3]`, and SQL/JSON literals all contain `[digit+]`. The edit parser is explicitly forgiving (`edit [6]`, `Edit [6]`, `[6]`, `fix position 6`) — which compounds the problem. A model emitting prose like "position [6] in the list" could be parsed as an edit op.

**Fix:** use a glyph that can't appear in code: `§6`, `⟨6⟩`, or a fenced marker like `[@6]`. Then tighten the parser — "forgiving" should mean tolerating whitespace and capitalization, not accepting three different syntactic structures. Keep forgiving parsing as a diagnostic mode that emits a warning, not the default.

### 4. IR symbols have collisions, aren't escape-safe, and don't round-trip through strings

Several real hazards visible in `ir_shim.py` that the spec doesn't acknowledge:

- **Intra-language collisions:** `GO_SYMBOLS` uses `ν` for both `range` and `var`; `RUST_SYMBOLS` uses `δ` for `default`. Cross-language reuse is fine *if* the shim is always language-scoped, but the current reverse maps are built per-lang without collision detection.
- **String literal contamination:** `β` inside a docstring, comment, or user-facing string gets expanded to `class` on the way out. Greek math in ML code, "λ-calculus" in a comment, Python's `α` coefficient — all corrupt.
- **Tokenizer variance:** cl100k_base may tokenize Greek as 2 bytes each. Qwen's tokenizer tokenizes many Greek letters as single tokens. If the LoRA is Qwen-based but benchmarks use tiktoken, the savings numbers are wrong.

**Fix:**

1. Tokenize before substitution (AST/CST) — never do string-level substitution inside string/comment/template-literal nodes. `ozmalib.compress.stub_functions` already has respect-for-strings behavior; reuse it here.
2. Assert collision-free symbol maps in CI (`assert len(set(map.values())) == len(map.values())`).
3. Publish per-tokenizer token counts for each symbol table. Add a `--tokenizer` flag so the shim can fall back to ASCII bigrams for tokenizers where Greek is worse than the keyword.

### 5. No formal grammar — everything is prose + examples

Every data type is "here's what it looks like". There's no EBNF, no JSON schema, no railroad diagram. The document calls itself a **wire protocol** — wire protocols need grammars. Without one:

- Training data and shim parser will drift.
- "Bidirectional" outputs from the model can't be distinguished from near-misses.
- Version 2 has nothing concrete to diff against.

**Fix:** append a `§Grammar` section with EBNF for each data type. At minimum: EDIT, INTERACT commands, DELTA, HINT, OUTLINE. Generate the parser and validators *from* the grammar so the spec is executable. For bidirectional types, publish a JSON schema + a normalization function.

### 6. Capability fingerprinting is missing

Probe announces capabilities by name. A v1.0 LoRA and a v1.2 LoRA may both ack `OUTLINE`, but their outline format diverges. The shim has no way to know.

**Fix:** each capability in the probe carries a fingerprint (`OUTLINE:a4f9`) — hash of the grammar spec. Model acks the hash it was trained against. Shim enables the capability only on exact match; mismatch falls through to L1 prose for that type while leaving other capabilities intact.

---

## Medium-impact issues

### 7. Negotiation is one-way and one-shot

"The negotiated level is fixed for the session. No mid-session upgrades." Also no downgrades: if the L3 model starts producing invalid DELTA, the shim can't fall back to treating outputs as prose. Over a 200-turn session that's a lot of broken validation with no recovery.

**Fix:** let validation failures per-capability disable *that capability* for the session (circuit-breaker). Log + report to the client. The session stays at L3 for EDIT and OUTLINE even if DELTA flakes.

### 8. DELTA is expensive and redundant

The system can compute the diff itself. Asking the model to also emit one costs output tokens and relies on the model being honest. The spec says "the system validates by computing the actual diff and comparing" — the diff exists anyway. What does DELTA add? Only the *labels* (`modified: added empty-file guard`).

**Fix:** drop DELTA-as-full-enumeration. Ask instead for per-change *rationales* keyed to positions the shim already detected changed: `rationale [6]: added empty-file guard`. Less output, same signal, cheaper to validate.

### 9. WIRE is the weakest capability

- Abbreviation table is an informal bag with no versioning.
- Savings on modern BPE are marginal — `database` → `DB` saves maybe 1 token on cl100k_base, nothing on some tokenizers.
- Wire instructions are injected at L3 only, but the abbreviation table in `wire.py` is what the LoRA was trained against. If those drift, expansion silently drops detail.
- "Skip filler" is a prose instruction, not a protocol rule — can't be validated.

**Fix:** decide whether WIRE is a capability or a *stylistic* layer. If a capability: version the abbreviation table (`WIRE:v3`), ship it as part of the system prompt, and validate round-trips on a held-out prose set (lossy expansion is a regression). If stylistic: move it out of the protocol and into a training-data note.

### 10. `ask "question"` has no transport

INTERACT includes `ask "question"` — "blocks on human input". What does that mean when the client is a CI pipeline, a batch job, or a headless agent? The spec doesn't define the client-side contract. Does the shim return 202? Poll? Error out? Fall back to `done stuck`?

**Fix:** `ask` returns a distinct response type the client must handle. Define it: `{"status": "needs_human", "question": "...", "session_id": "..."}`. Also split `done` into `done success | done partial | done blocked` so batch clients can distinguish completion from surrender.

### 11. Ack detection window is wrong

64 characters can miss a real ack behind a single `<thinking>` tag and accept a spoofed ack surrounded by whitespace. Line-based detection is more robust.

**Fix:** scan the first N *lines* (say 3), skipping blank lines, markdown headings, thinking blocks, and known non-semantic prefixes. Document which prefixes are stripped.

### 12. System prompt injection isn't cache-friendly

At L2+ the shim injects IR legends into the system message. If they change per-request (detected language depends on input), it invalidates the Anthropic/OpenAI prompt cache every turn. `why-compress.md` and the project CLAUDE.md both care about cache hit rate.

**Fix:** inject the *full* legend (all languages) once per session, unconditionally. The extra bytes are trivial and the cache hit is worth orders of magnitude more. Or: one stable system block + one dynamic user-level block with per-turn state.

---

## Smaller points

- **Ack in §Ack is wrong for L2 vs L3 precision:** the spec example uses the full capability list for L3. But a L2 model (IR-only LoRA) would correctly ack `IR` — except the probe doesn't list `IR` in the protocol capability list in the same form. Align the cap names.
- **§Shim Responsibilities table:** "Parse ack" is marked ✓ for L2 and L3 only. But the shim has to *try* to parse ack at every level to know whether to stay at L1 — the row should be ✓ for all levels; what varies is what it does with the result.
- **Minification via `<legend>`:** aliasing `user_authentication_service` → `a1` in model output is unsafe if the model hallucinates `a2`. Need explicit validation that every alias the model emits was declared.
- **No telemetry schema:** for a LoRA feedback loop you want structured events: ack observed, capability used, validation passed/failed, expansion succeeded. Define the event shape in the spec so client implementations emit compatible data.
- **Versioning:** `[OZMA v1 ...]` vs `[OZMA v2 ...]` is major-only. Add minor (`v1.3`) so additive capability extensions don't require a major bump.
- **§Extension Points — `X-Ozma-Level: 1` override:** useful. Add `X-Ozma-Caps: OUTLINE,EDIT` (subset override) and `X-Ozma-Validate: strict|lenient|off`.
- **No error type:** if the model refuses or emits a guardrail response, the shim has no structured way to tell the client. Define `ERROR` with `{kind, position?, message}`.
- **Training format §2 Correction:** the corrected assistant turn doesn't re-emit the ack header. Explicit rule needed: ack once per session, never again.

---

## Priority

If forced to pick three to land first:

1. **Stable position IDs (§2)** — biggest correctness lift; multi-turn currently has a silent bug.
2. **Formal grammars + capability fingerprints (§5, §6)** — the difference between a protocol and a convention.
3. **String-safe IR via AST-aware substitution (§4)** — otherwise a single unicode-containing docstring can corrupt a patch.

Everything else is polish compared to these.
