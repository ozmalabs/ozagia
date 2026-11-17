# Ozma Protocol v1.4 — Re-Review

**Reviewer:** Claude (Opus 4.7)
**Date:** 2026-04-19
**Target:** `protocol.md` v1.4 (2026-04-19)
**Priors:** `protocol-review.md` (v1.0 review), `protocol-improvements.md` (v1.0 forward-looking)

The spec has grown from 693 to 1607 lines across v1.1–v1.4. Every item from
`protocol-review.md` is addressed and every proposal from
`protocol-improvements.md` is incorporated. That is unusually thorough
follow-through — and it surfaces a new layer of issues that weren't
visible at v1.0.

---

## What landed cleanly

- **Handshake spoofing / behaviour coupling** — fixed. Registry is source
  of truth; HMAC challenge-response; line-based ack detection with prefix
  skipping.
- **Position stability** — fixed via `§N=STABLE_ID` (sha256 of qualified
  symbol). Parser accepts both ordinals and stable IDs; the latter survive
  edits.
- **`[N]` collision** — fixed via `§`. Legacy `[N]` now diagnostic-mode only.
- **String-safe IR** — fixed. AST-level substitution specified; collision
  CI check; per-tokenizer symbol selection; empty-string strip sentinel
  for C++ access modifiers.
- **Capability fingerprinting** — landed. Mismatch falls through to L1
  for that type only.
- **DELTA redundancy** — replaced by RATIONALE; shim computes actual diff.
- **Cache-friendly system prompt** — full IR legend injected
  unconditionally; per-turn state moved to user-role prefix.
- **Session cache** — fully specified with `{@HASH}` content addressing,
  `@prev`/`@target`/`@last_edit` anaphora, cross-turn deltas.
- **CLAIM, REASONING, PLAN, TEST-SPEC, HANDOFF, TRANSACTION, pattern
  compression, preview, semantic search, constrained decoding** — all
  present with grammar entries and training examples.

This is a v1.4 that is structurally shippable. The remainder is polish
and a handful of real problems.

---

## New issues surfaced by the re-review

### 1. The EBNF has duplicate definitions (blocking)

`EDIT_CMD`, `INT_CMD`, `EDIT_LIST`, `CONFIDENCE`, `CAP_PAIR`, and `ACK`
are each defined twice in §Formal Grammar. The second definition is a
superset (adds confidence, preview, transaction, semantic search, hash
refs). Two problems:

- **The grammar doesn't parse cleanly.** A generator reading top-to-bottom
  will bind the first definition; anything relying on the second is
  silently wrong.
- **Fingerprints break.** Capability IDs are `sha256(grammar_section)[:4]`.
  If the grammar has two `EDIT_CMD` definitions, what's the canonical
  section? Different implementations will hash differently and fail ack.

**Fix:** merge each duplicate into a single authoritative definition;
publish the grammar as one linear EBNF file the fingerprinter consumes;
make it CI-checked that no name is defined twice.

### 2. The full `TURN` production is weakly specified

```
TURN = [ACK], [REASONING], [RAT_BLOCK], [CLAIM_BLOCK], [PLAN],
       (EDIT_LIST | INT_LIST | OUTLINE | HINT_BLOCK | GRAPH_OUT
        | TREE_OUT | TEST_SPEC | HANDOFF_TO),
       [TEST_SPEC];
```

- **Rigid ordering** pre-commits the training target (reasoning first,
  then rationale, then claims, then plan). Is that actually the natural
  emission order? Claims about a position might naturally follow the
  edit that established them.
- **`TEST_SPEC` appears twice** (main body + optional trailer) —
  intentional, or stale edit?
- **Main body is exclusive** — model can emit edits OR interact commands,
  not both. But turn-level mixing is useful (`callers §6` + `edit §6`
  in one turn). Training example §3 splits them across turns so the
  grammar is consistent, but the *protocol* doesn't discuss whether
  this is a deliberate training invariant or a limitation.
- **`HANDOFF_FROM` is missing** from the main body. Probably intentional
  (it's input-only) but worth an explicit comment.

### 3. CONFIDENCE is advertised as universal but only applied to edits

The improvements doc asked for confidence on "all output types." The
spec adds it to `EDIT_CMD` and references it for CLAIM, but:

- REASONING chains don't carry per-hypothesis confidence (the obvious
  place for calibration — was `h1 → reject` a high-confidence rejection
  or a guess?).
- PLAN steps don't carry confidence (useful for "this step is likely
  to fail, review before executing").
- HINT entries already had confidence pre-v1.2, which is inconsistent
  with how it's wired into EDIT.

**Fix:** Single normative `CONF_ATTR` that attaches anywhere a scoreable
assertion appears. Grammar should make this uniform.

### 4. Pattern compression detection is under-specified

"When the stub pass identifies N functions with identical structure
(same IR shape, varying identifier names), the shim renders them as a
named pattern."

What does "identical structure" mean? AST shape equality? Tree-sitter
node-type sequence match? Semantic equivalence (same call graph)?
Threshold for "similar enough"? The detection heuristic determines
whether pattern compression is safe or produces miscoded edits.

Related: `edit P1: body` applies the edit to *all* members. No syntax
for "edit members §13 and §14 but not §15". Needed for partial-pattern
edits, which are a real case.

### 5. HANDOFF code-state delivery is unclear

"The sha256 of concluded patches lets the shim verify the next model
is reasoning about the correct code state." — but *what does the next
model see* of those patches? Options:

- Full post-patch state in context (defeats compression).
- `{@HASH}` reference that expands to current state (requires
  cross-model session cache).
- Just the sha, with the model expected to `expand §N` if it cares (slow).

For multi-model pipelines spanning processes/hosts, session cache
synchronization is non-trivial. The spec implies option 2 but doesn't
commit to it or specify the sync mechanism.

### 6. Circuit-breaker semantics need tightening

"N consecutive turns (default N=3)" — but:

- What's "consecutive"? Turns 1/3/5 fail with 2/4 passing — does that
  count?
- Does circuit-breaker persist past session end? Probably not, but not
  stated.
- Can the client re-enable a disabled cap? Presumably via `X-Ozma-Caps`
  override, but should be explicit.
- Does a corrected-retry turn count as a failure if the first attempt
  failed?

### 7. Session cache invalidation/eviction is partial

§Session Cache handles the *edit* case (pre/post-patch fingerprints
diverge). Not handled:

- **External mutations** between turns (git pull, user edited file in
  IDE). The fingerprint is stale but nothing detects it.
- **Partial invalidation.** One function changed; most of the outline
  is still valid. Re-rendering the whole chunk is wasteful.
- **Eviction policy.** Session cache grows unbounded. Long-lived sessions
  need LRU or size caps.
- **Cross-session persistence.** Is `sha256(chunk)` stable across
  restarts? Implied yes but not specified.

### 8. Streaming is absent from the normative spec

`ir_shim.py` has streaming support but §Data Types / §Shim
Responsibilities treats everything as request/response. Edge cases that
matter:

- Mid-stream `{@HASH}` expansion (possible if a chunk boundary falls
  inside a reference).
- Ack header arriving after a non-blank thinking-block line — can the
  shim commit to level confirmation before the full first response
  arrives?
- TRANSACTION spanning multiple streaming chunks — when does the shim
  know it's complete before `end T1` lands?
- Cap circuit-breaker triggering mid-stream — what does the client see?

### 9. CLAIM verification is aspirational

"Auto-generate pytest test cases from the claim text via a cheap model"
/ "Run available property-test harnesses (Hypothesis, proptest) against
it" — easier specified than built. The spec says "Pass claims without
verification — and record the verify status separately" which is the
right fallback, but the protocol should be explicit that **most claims
will be `unverified` in practice, and that is not a failure**. Also:
how does the test-gen model map "never raises on malformed JSON" to a
concrete test? That's a small language-model task of its own and should
have its own protocol entry or a pointer to one.

### 10. The v1.4 language explosion introduces new symbol hazards

- **Haskell `{=do` and Lua `{=do`, `}=end`:** single-char braces
  substituting for keywords is fine at AST level but fragile if the
  language has actual structural braces (Haskell record syntax, Lua
  table literals). The AST-level rule must cover this exactly; a single
  tree-sitter misclassification corrupts output.
- **Nix `ω=with` and Haskell `ω=where`:** cross-language reuse is
  allowed but embedded languages (Haskell in a Nix `writeText` string)
  may confuse the scoping.
- **Bash `∘=do`, `∫=done`, `∑=esac`:** clever mnemonics but obscure
  unicode. Tokenizer cost hasn't been validated across qwen/cl100k/etc.
  The per-tokenizer drop rule should catch this, but the spec should
  acknowledge that some languages may end up with partial IR depending
  on tokenizer.
- **Terraform/HCL and Dart** fall back gracefully to "no tree-sitter
  stub collection, IR abbreviation table only." Good, but that means
  `§N` position addressing doesn't work in those files. The spec should
  say explicitly that EDIT/INTERACT falls back to L1 semantics for
  these languages until tree-sitter support is available.

---

## Smaller nits

- **Legend state across turns:** per-request `<legend>` blocks minify
  for that turn, but the session cache can return chunks from turns
  with different legends. Stale alias expansion is possible. Spec
  should require legends be identity-stable within a session or aliases
  be session-scoped.
- **Telemetry omissions:** no tokenizer name, no upstream prompt-cache
  hit metric, no streaming/non-streaming flag, no hardware indicator.
  These matter for cross-deployment comparison and LoRA retraining
  analysis.
- **Training opt-in filtering:** not every turn from an opt-in session
  is useful training signal. The pipeline should filter (e.g., drop
  turns with `ack_hmac_fail`, drop refused turns). Mention in
  §Training-data capture.
- **Error type enumeration incomplete:** `"kind"` is a short list — no
  entry for session-cache failures, legend-lookup failures, fingerprint
  mismatches, streaming interruption. Needs to enumerate all reachable
  shim errors.

---

## Still-missing improvements (worth a v1.5+ consideration)

From the improvements doc, these are *not* yet incorporated:

- **Learned BPE extension** (repo-specific tokenizer add-ons). Still a
  future direction.
- **Attention-aware compression via telemetry feedback** — telemetry
  exists now but no feedback mechanism back into the compression pass.
- **Docstring-specific compression.**
- **Cross-session persistent memory** — session cache is scoped to a
  session; no `recall "thing I worked on yesterday"` primitive.
- **Embedding references as first-class citizens** — `search semantic`
  is now a command but there's no `@semantic:<embedding-id>` reference
  or ability to emit/consume embeddings structurally.
- **Multi-model shared cache** — relates to HANDOFF gap above.

None of these are blockers. They're the next layer once v1.x is shipped
and the telemetry starts producing feedback data.

---

## Overall

The spec is in strong shape. v1.4 is structurally ready to ship; the
blocking issues are:

1. **Fix the EBNF duplicates** — this breaks grammar fingerprinting,
   which gates the entire capability-mismatch-fallback mechanism.
2. **Decide `TURN` grammar** — either justify the rigid ordering or
   relax it to a set. Remove the duplicate `TEST_SPEC`.
3. **Commit on HANDOFF code-state delivery** — specify the mechanism,
   even if it's "full state for now."
4. **Specify circuit-breaker semantics** — consecutive, persistence,
   re-enable.
5. **Add streaming section** — at least a paragraph per data type about
   streaming invariants.

Everything else on my list is v1.5 polish. The protocol has moved from
"napkin spec" to "reviewable standard" in one revision cycle, which is
remarkable follow-through.

---

## Priority for a v1.5

| Priority | Fix | Why |
|----------|-----|-----|
| P0 | Merge duplicate EBNF definitions | Grammar fingerprinting broken otherwise |
| P0 | Commit TURN grammar (ordering, main-body exclusivity, remove duplicate TEST_SPEC) | Training target must be unambiguous |
| P0 | HANDOFF code-state mechanism | Multi-model pipelines can't work without it |
| P1 | Circuit-breaker: define "consecutive", persistence, re-enable | Runtime behaviour under failure must be deterministic |
| P1 | Streaming invariants per data type | Real deployments stream |
| P1 | Session cache eviction + external-mutation invalidation | Long sessions break without this |
| P1 | Universal CONFIDENCE | Currently inconsistent across types |
| P2 | Pattern compression detection semantics | Prevents miscoded edits |
| P2 | Legend session-scoping rules | Stale alias hazards |
| P2 | Telemetry schema: tokenizer, streaming flag, upstream cache | Cross-deployment comparison |
| P2 | Error type kind enumeration | Complete failure mode coverage |
| P3 | L1 fallback note for Dart/HCL/Terraform (no tree-sitter) | Explicit limitation |
| P3 | Training opt-in filtering guidance | Data-pipeline hygiene |
