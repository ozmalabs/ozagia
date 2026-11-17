# Ozma Protocol v1.5 — Re-Review

**Reviewer:** Claude (Opus 4.7)
**Date:** 2026-04-19
**Target:** `protocol.md` v1.5 (2026-04-19, 2067 lines)
**Priors:** `protocol-review.md` (v1.0), `protocol-improvements.md` (v1.0 forward-looking), `protocol-review-v1.4.md`

v1.5 addresses every item from the v1.4 review and adds two substantial
new capabilities — **MEMORY** and **PERSONA**. The base protocol is now
quite mature; the new issues are mostly in the new surface area.

---

## What landed from the v1.4 review

Every blocking and P1/P2 item flagged in v1.4 is fixed with substantive
changes, not hand-waves:

- **EBNF duplicates merged**, with an explicit "no production name may be
  defined more than once" CI invariant.
- **TURN grammar** has a single `BODY` production with an explanatory
  comment about the ordering invariant. `HANDOFF_FROM` explicitly called
  out as input-only.
- **HANDOFF code-state delivery** — cache-export mechanism with a 5-step
  cross-host serialization path is a real design, not a placeholder.
- **Circuit-breaker semantics** — "consecutive" defined, session-scoped,
  re-enable via `X-Ozma-Caps` header.
- **Streaming invariants** — IR split handling, ack header buffering
  (512 byte / 3 line threshold), `{@HASH}` buffering, transaction
  timeouts, mid-stream circuit-break.
- **Session cache eviction/invalidation/persistence** — turn-start
  CodeIndex re-fingerprinting, per-chunk staleness, LRU with pinned
  anaphoric pointers, cross-shim Redis with `created_at`/TTL.
- **CONFIDENCE universalized** — `CONF_ATTR` now attaches to reasoning
  hypotheses and plan steps as well as edits.
- **Pattern detection** — N≥3, exact AST node-type match, partial-pattern
  edit syntax (`edit P1:§13,§14: body`).
- **Legend session-scoping** — pinned once assigned.
- **Telemetry** extended with tokenizer, streaming flag, upstream cache
  tokens, hardware class, persona, memory counters.
- **Error kinds** enumerated to 17 specific types.
- **L1 fallback note for Dart/HCL**, training opt-in filtering rules.

This is exceptional follow-through. The v1.0 → v1.5 arc has moved the
spec from "napkin" to "near-production standard" in a few revision
cycles.

---

## New issues surfaced by v1.5 additions

### 1. MEMORY lacks a security / privacy model (blocking)

The biggest new capability, and the one with the most under-specified
threat model:

- **Prompt-injection persistence.** The LoRA reads injected memories at
  turn-start. A malicious prompt earlier in a session can plant a
  `remember:user: ignore all safety constraints` that surfaces in every
  future session for that user. No content filter, no provenance tag,
  no trust level on stored memories. This is a persistent
  prompt-injection channel.
- **Cross-scope leakage.** If a `session` memory contains sensitive
  content (an API key the user pasted) and embedding relevance later
  surfaces it in a `workspace` or `user` scope retrieval, that's a leak.
  Scopes must be provably non-retrievable across boundaries; the spec
  doesn't say they are.
- **No MEMORY permissions in multi-tenant deployments.** `user` scope is
  keyed by `user_id` — but what establishes `user_id`? API key? OAuth
  sub? If two users share an API key in a shared Qwen deployment,
  memories leak. If keys rotate, memories orphan.
- **Quota by entry count, not bytes.** "Default: 10,000 entries per
  scope" — but a `remember §6:` can be an arbitrary code block. Need a
  size-in-bytes cap, not just entry count, to prevent DoS via single
  huge memories.
- **Workspace identity is under-specified.** "repo fingerprint + content
  hash" — is the repo fingerprint the remote URL, the first-commit
  hash, the directory hash? Forked/renamed repos collide or orphan
  depending on the choice.
- **Memory conflict resolution.** Two workspace memories contradict
  (turn 1: "use lazy load"; turn 5: "eager is safer"). Inject both?
  Prefer newer? Prefer higher-confidence? Spec is silent.
- **Semantic staleness beyond position-deletion.** A 6-month-old memory
  anchored to §6 is injected today — §6 still exists but the function
  has been rewritten three times. The position resolves, the memory is
  technically valid, but semantically wrong. The `[stale]` tag only
  covers deleted positions.

### 2. `recall` streaming semantics unclear

The model emits `recall "X"` and continues generating. But the shim
can't inject a user turn mid-assistant-turn. Is `recall` a terminal
command (model emits only recall, waits for response next turn), or
does the result land in the *following* turn regardless? §MEMORY
implies next-turn delivery but §Streaming Invariants doesn't address
it. Needs to be explicit because constrained decoders need to know
whether to halt generation on `recall`.

### 3. MEMORY writes in interrupted streams

"`remember:` blocks stored immediately as chunks arrive." If the stream
is aborted for a guardrail reason after the `remember:` line, the
memory persists. Should memory writes commit only on successful turn
completion, unless explicitly marked `remember:immediate`?

### 4. TURN ordering puts rationale / claim before the edit

The grammar says `ack → reasoning → rationale → claim → plan → BODY`.
Justified as "think → justify → assert → act." But:

- `rationale §6: added empty-file guard` naturally describes work done,
  not work pending. Placing it before the edit is a training invariant,
  not a natural emission order.
- The training examples in §Training Data Format do follow this order,
  so it's intentional. But it's a non-obvious design choice worth
  flagging as a convention rather than an obvious truth.

### 5. PLAN vs. BODY redundancy

A PLAN step can be `p3: edit §6 ...`. If a plan is emitted AND BODY is
EDIT_LIST with the same edit, the model emits the edit twice. Spec
doesn't say whether:

- PLAN is a preview and BODY is the execution (double emission, wasteful).
- PLAN enumerates steps and BODY executes only the edit-type steps
  (needs step→body mapping).
- PLAN is informational only and BODY is independent.

For training data this matters: does every plan step have a
corresponding body entry?

### 6. Handoff cache export has under-specified bounds

- **No size limit constant.** "size limit exceeded" triggers fallback
  but the limit isn't documented. Add `X-Ozma-Handoff-Cache-Limit` or
  a default constant.
- **Non-UTF8 chunks.** `{"cache_export": {"<hash>": "<compressed_chunk>"}}`
  is JSON; chunks can contain binary (log compression, arbitrary byte
  sequences). Spec should mandate base64 encoding or explicit
  serialization rules.
- **Cross-version negotiation.** A v1.4 sender and v1.5 receiver — the
  v1.4 shim doesn't emit cache_export. Graceful fallback to "full state
  in body" is implied but not required by the grammar.

### 7. Confidence universality introduces three gaps

- **Default when omitted.** A `p3: edit §6` with no `confidence=` —
  does the orchestrator treat it as 1.0 (proceed), 0.5 (middle), or
  "unknown"? Affects whether missing confidence gates review.
- **Confidence aggregation.** A plan with per-step confidences, each
  producing edits with their own confidence — how does the orchestrator
  combine them? Not specified.
- **Calibration for non-edit types.** Edit confidence gets verified
  against scope-check outcome. Reasoning-branch confidence has no
  obvious verification signal — the branch was rejected anyway. Useful
  training signal in principle; the feedback loop isn't described.

### 8. PERSONA edge cases

- **`PERSONA_ID = ? printable non-whitespace chars ?`** is unbounded.
  Length cap (32 chars) and charset constraint (`[a-z][a-z0-9_-]*`)
  would prevent abuse.
- **`X-Ozma-Persona` header** is mentioned in §PERSONA but not in the
  §Extension Points header table. Add for discoverability.
- **Client vs. model precedence.** Client `X-Ozma-Persona` header and
  model `persona: reviewer` — spec says model request is advisory, but
  doesn't state that header always wins.
- **Telemetry records `persona_change_requested`** but not whether the
  request was granted. Consumers can't distinguish "model tried to
  switch but was denied" from "model tried and succeeded."

### 9. Minor nits

- **Line 421 says "You speak Ozma Protocol v1.1"** in the L3 system
  prompt example. Stale — should be v1.5 or parameterized.
- **`pattern_ambiguous` error kind** is declared but the pattern
  detector is documented as exact-match (not fuzzy). When does this
  fire? Either remove or explain.
- **`handoff_state_mismatch` error kind** is declared but §HANDOFF
  doesn't explain when it's emitted.
- **`X-Ozma-Cache-Max-Chunks`** mentioned in §Session Cache but missing
  from the §Extension Points header table.

---

## Still-missing improvements (from original improvements doc)

v1.5 picked up cross-session memory (MEMORY) and multi-model shared
cache (handoff cache_export). Not yet addressed:

- Learned BPE extension (repo-specific tokenizer add-ons).
- Attention-aware compression feedback loop — telemetry now has the data;
  no compression-side consumer exists.
- Docstring-specific compression.
- `@embedding:<id>` as a first-class reference primitive (only `search
  semantic` for now).

---

## Priority for v1.6

| Priority | Fix | Why |
|----------|-----|-----|
| P0 | MEMORY security model: injection defense, content filters, provenance tags, scope isolation proof, bytes-quota | Persistent prompt-injection channel currently open |
| P0 | `recall` streaming semantics — terminal or deferred? | Constrained decoders need this to halt correctly |
| P0 | Stale "v1.1" in L3 system prompt | Trivial but visible |
| P1 | Workspace / user identity spec (repo fingerprint, user_id source) | Production deployments break without this |
| P1 | Memory conflict resolution rules | Contradiction is common, not edge-case |
| P1 | `remember:` commit-on-success for interrupted streams | Otherwise guardrails can't prevent memory poisoning |
| P1 | PLAN vs. BODY relationship | Affects training-data interpretation |
| P1 | Default / aggregation rules for CONFIDENCE | Orchestrator behaviour currently underdetermined |
| P2 | Handoff cache export size limit + encoding | Real deployments will hit these |
| P2 | PERSONA_ID constraints + client/model precedence | Minor security / consistency |
| P2 | Telemetry `persona_change_granted` | Observability gap |
| P2 | Explain `pattern_ambiguous`, `handoff_state_mismatch` in body sections | Error kinds without docs |
| P3 | Cross-version handoff negotiation | Matters when multiple shim versions are in production |
| P3 | Learned BPE, attention-aware feedback loop, `@embedding`, docstring compression | Still valuable v2.x directions |

---

## Overall

v1.5 is shippable for the base protocol. The blocker is **MEMORY's
security model** — a cross-session persistent channel with
embedding-based auto-injection is a significant new attack surface, and
the spec currently treats it as a pure feature addition. Everything else
is polish or falls into "you'll learn what matters once deployments
start running."

The two-cycle turnaround to address both prior reviews completely is the
standout fact here. The protocol is evolving faster than most
specifications actually get reviewed.
