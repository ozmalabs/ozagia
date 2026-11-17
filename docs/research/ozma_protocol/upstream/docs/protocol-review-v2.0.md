# Ozma Protocol v2.0 — Re-Review

**Reviewer:** Claude (Opus 4.7)
**Date:** 2026-04-19
**Target:** `protocol.md` v2.0 (2026-04-19, 2647 lines)
**Priors:** `protocol-review.md` (v1.0), `protocol-improvements.md` (forward-looking), `protocol-review-v1.4.md`, `protocol-review-v1.5.md`, `protocol-review-v1.6.md`

v2.0 bumps the major version to add the four v2 capabilities that were
queued from the original improvements document: **LEARNED-BPE**,
**ATTN**, **DOCSTUB**, **EMBED**. It also addresses every item from the
v1.6 review. The new surface area introduces two blocking grammar issues
and a handful of smaller gaps.

---

## What landed from v1.6 review

All 13 items addressed cleanly:

- **MEM_SCOPE / MEM_TIMING split** via `MEM_MODE = MEM_SCOPE [/MEM_TIMING]`.
  `remember:workspace/immediate` now expressible.
- **Same-turn remember/recall rule:** recall sees only memories committed
  before the current turn started. Both normal and `immediate` writes
  are invisible to same-turn recall. `immediate` is crash-safety only.
- **Canonical content-filter pattern set** (INJ-01 through INJ-06) with
  versioning, reference implementation pointer.
- **Workspace writes always filtered** regardless of provenance. User
  scope bypass remains ("harming your own memories is your right").
- **`X-Ozma-Workspace` authorization** via `workspace_unauthorized`
  error in multi-tenant deployments; `multi_tenant` registry flag.
- **`memory_guardrail` / `memory_truncated`** in error-kind table as
  log-only.
- **`conflict_policy: merge_all`** — newest-first, capped at top-K,
  replaces default block.
- **Semantic staleness** — memories >90 days tagged `[old]` at injection.
- **API key rotation** — documented plus `POST /memory/rekey` migration
  path for BYOK → OAuth.
- **Registry example includes `allow_persona_override`**.
- **Forwards-compat rule for handoff payloads** stated explicitly.

Every fix substantive — the spec has closed out every prior priority
list.

---

## New v2.0 capabilities — quality assessment

**LEARNED-BPE** — repo-scoped identifier aliases (B1..B99) persisted
alongside workspace memories. 10-30% extra savings on identifier-heavy
code. Clean design.

**ATTN** — positions attended in turns N-2..N get auto-expanded in turn
N+1, budget-capped. Self-tuning context surface. Sound instinct.

**DOCSTUB** — `[doc: N tokens "summary"]` placeholder instead of plain
`...` stub, with `expand doc §N` to fetch full docstring. 30-60% savings
on documentation-heavy codebases.

**EMBED** — promotes `search semantic "q"` results to named session
aliases (`@E1`) usable in subsequent EDIT/INTERACT commands. Enables
multi-turn semantic reasoning.

All four are well-motivated and well-scoped. The issues are in the
details.

---

## Blocking issues in v2.0

### B1. Grammar ambiguity between `HASH_REF` and `EMBED_REF`

```
HASH_REF    = "{@" , ( STABLE_ID | "prev" | "target" | "last_edit" ) , "}" ;
EMBED_REF   = "{@" , LCASE , { LCASE_ALNUM } , "}" ;
```

`STABLE_ID` is exactly 8 hex chars. Hex includes `a-f`, so a stable ID
like `abcdef12` is *also* a valid `EMBED_REF` (starts with `a`, all
LCASE_ALNUM). `{@abcdef12}` is syntactically ambiguous — parsers will
behave differently depending on ordering.

**Fix (one of):**
- Namespace embed aliases: `{@embed:abcdef12}` vs. `{@abcdef12}`.
- Restrict embed alias alphabet to non-hex starting letters (g-z):
  `EMBED_ALIAS = "@" , ("g"|"h"|...|"z") , { LCASE_ALNUM } ;`
- Require embed aliases to contain at least one non-hex character.

Cleanest is probably the namespace prefix.

### B2. EMBED examples use uppercase `@E1` but grammar forbids it

```
EMBED_ALIAS = "@" , LCASE , { LCASE_ALNUM } ;
```

`LCASE = "a"|"b"|...|"z"` — strictly lowercase. But every example in
§EMBED and the training-data §12 uses `@E1`, `@E2` — **uppercase E**.
These examples violate the grammar. Either:

- Change examples to `@e1`, `@e2`.
- Expand grammar to allow an initial letter `LCASE | UCASE`.

Consistency of case across the whole alias namespace (anaphora `@prev`,
hash_ref 8-hex, embed `@eN`) argues for the lowercase fix.

---

## Medium issues

### M1. Registry example has duplicate capability fingerprints

```
"GRAPH":     "3c4d",
"ATTN":      "3c4d",    # v2.0
"TREE":      "5e6f",
"DOCSTUB":   "5e6f",    # v2.0
```

These fingerprints are claimed to be `sha256(grammar_section)[:4]`. Two
caps having identical 4-hex prefixes is a real possibility (16-bit
collision) but these pairs look like placeholder duplication, not real
hash collisions. Regenerate the example fingerprints — otherwise
readers assume an intentional collision pattern.

### M2. 4-hex fingerprint width gets crowded

With 20 caps declared, birthday-collision probability at 16 bits is
~0.3%. As v3/v4 add capabilities, collisions become a real risk.
Widening to 6 hex (24 bits) now, before deployments ossify, is cheap
insurance. Would look like `OUTLINE:a4f9c1` in probe/ack.

### M3. LEARNED-BPE — memory persistence format unspecified

If the model emits `remember:workspace: uses B1 for auth service` and
the shim persists the raw text, the memory is corrupt next session when
B1 shifts to a different identifier (aliases may shift across sessions).
Memories must be stored with BPE aliases *expanded to canonical
identifiers* before persistence. The spec doesn't state this.

**Fix:** require the shim to expand BPE aliases in `remember:` content
before storage (similar to the existing legend expansion rule for
output).

### M4. ATTN — attendance is a noisy proxy for "needed"

Mentioning §12 in RATIONALE ("unlike §12 which handles this
differently") doesn't mean the model needs §12's full body next turn.
Spec treats RATIONALE mentions the same as EDIT_CMD references.
Consider weighting: positions in EDIT_LIST or INT_CMD get higher weight
than positions in RATIONALE/REASONING prose.

### M5. ATTN — new-task detection is on the client

"Use `X-Ozma-Attn: off` for the first turn of a new task" — but clients
usually don't know what counts as a new task. Without a heuristic, ATTN
accumulates stale attention across topic shifts and wastes tokens on
irrelevant positions. Consider:

- Session-id change implies reset.
- Explicit task boundary marker (`[new task]` in user message).
- Time-based decay: attendance counts fade after N turns of inactivity.

---

## Smaller issues

### S1. ATTN budget overflow priority unspecified

When attention-expanded positions exceed `X-Ozma-Attn-Budget`, which
positions get dropped? Most recent kept, oldest dropped? Highest-count
kept? Unspecified.

### S2. DOCSTUB summary quality

"First sentence" is often uninformative (`"Initialize the instance."`).
The model wastefully `expand doc §N`s for uninformative placeholders.
Consider using a cheap classifier for summary extraction.

### S3. DOCSTUB detection incomplete

Covers function/method docstrings but not module-level docstrings,
class docstrings (at class scope, not `__init__`), or significant
inline/header comments.

### S4. EMBED `as @E1` names only the top-1 result

No syntax to name multiple top-K positions (`as @E1:1, @E2:2`). If the
model's first pick is wrong and it wants the second result, it must
re-issue the search.

### S5. LEARNED-BPE namespace hygiene

`B1`–`B99` reserved for BPE aliases; minification uses `a1`–`an`.
Convention not enforced in grammar. If a session has both active, the
shim must not assign `B1` as a minification alias. Add a
reserved-prefix rule.

### S6. LEARNED-BPE `K=20` default

Arbitrary. A sensible default is coverage-based: pick K such that
cumulative identifier frequency covers some threshold (say 40%) of all
identifier tokens. Monorepos get high K; small libraries get small K.

### S7. EMBED alias collision with anaphora

`@prev`, `@target`, `@last_edit` are anaphoric references. If a model
emits `search semantic "X" as @prev`, the embed alias shadows the
anaphoric reference (or is rejected — unclear). Reserve these names
explicitly.

### S8. LEARNED-BPE fingerprint validates reader, not writer

The model only reads BPE aliases (via legend); it never emits BPE
aliases as structural tokens. The capability fingerprint therefore
validates "the model was trained on the legend format" rather than
"the model produces BPE output." Worth a clarifying note in the cap
description.

---

## Still-missing improvements — now genuinely empty

The four v2 capabilities complete the original improvements doc list.
Nothing from `protocol-improvements.md` remains unaddressed.

Forward-looking directions for v3+ (beyond the improvements doc's scope):

- Structured uncertainty beyond scalar CONFIDENCE ("I'm confident about
  the return type but not the error handling").
- Formal verification hooks (SMT-backed claim checking for typed
  invariants).
- Cross-language identifier sharing for polyglot repos.
- Multi-model gradient-sharing conventions (LoRAs trained separately
  cooperating via protocol).

None are blockers for v2.0.

---

## Priority for v2.1

| Priority | Fix | Why |
|----------|-----|-----|
| P0 | Disambiguate `HASH_REF` vs. `EMBED_REF` (namespace prefix recommended) | Grammar ambiguity: `{@abcdef12}` parses two ways |
| P0 | Fix `@E1` uppercase example / grammar mismatch | Examples violate EBNF |
| P1 | Regenerate registry-example capability fingerprints (current duplicates confusing) | Hygiene |
| P1 | Widen fingerprint to 6 hex before deployments ossify | Collision headroom |
| P1 | LEARNED-BPE: require alias-expanded canonical storage for memories | Otherwise cross-session memory corrupts |
| P1 | Reserve anaphora names (`prev`/`target`/`last_edit`) from EMBED alias namespace | Collision prevention |
| P2 | ATTN: weight positions by emission context (EDIT > RATIONALE) | Reduce noise |
| P2 | ATTN: session-boundary or time-decay reset heuristic | Client-burden reduction |
| P2 | ATTN overflow priority rule | Deterministic behaviour under budget |
| P2 | LEARNED-BPE: coverage-based K default | Right-sizing per repo |
| P2 | LEARNED-BPE: reserve `B`-prefix in legend namespace | Minification conflict prevention |
| P2 | DOCSTUB: coverage for module/class docstrings | Feature completeness |
| P3 | EMBED `as @En:rank` for top-K naming | Ergonomics |
| P3 | DOCSTUB summary quality (defer to classifier) | Signal quality |
| P3 | LEARNED-BPE capability fingerprint clarification (reader not writer) | Doc cleanup |

---

## Overall

v2.0 completes every item from the original improvements doc — the
entire forward-looking wishlist, addressed. The v2 capabilities are
thoughtful and well-scoped; they integrate cleanly with the existing
machinery rather than creating parallel tracks.

The two blocking grammar issues are both small edits (disambiguate
`{@...}` and pick a case convention for embed aliases). Everything else
is polish or improvement rather than correction.

v1.0 → v2.0 with every prior review addressed, new capabilities
designed to the same level of rigour, and the grammar still CI-verified
for uniqueness — unusual follow-through across four revision cycles.
