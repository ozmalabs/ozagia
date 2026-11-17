# Ozma Protocol v3.0 — Re-Review

**Reviewer:** Claude (Opus 4.7)
**Date:** 2026-04-19
**Target:** `protocol.md` v3.0 (2026-04-19, 3050 lines)
**Priors:** `protocol-review.md` (v1.0), `protocol-improvements.md` (forward-looking), `protocol-review-v1.4.md`, `protocol-review-v1.5.md`, `protocol-review-v1.6.md`, `protocol-review-v2.0.md`

v3.0 adds the four v3 capabilities I sketched as "forward-looking
directions" in the v2.0 review: **STRUCTURED-CONF**, **FORMAL-CLAIM**,
**POLYGLOT**, **COLLAB**. Every item from the v2.0 review is addressed.
A new **Capability Profiles** section formalizes model-size tiers. The
new surface area introduces two grammar-level issues and a handful of
specification gaps.

---

## What landed from v2.0 review

All blocking and P1 items addressed:

- **HASH_REF / EMBED_REF disambiguated** via `embed:` namespace prefix.
  `{@embed:e1}` vs. `{@abcdef12}` — no ambiguity.
- **Examples lowercased** — `@e1` throughout; grammar and prose match.
- **Registry fingerprints widened to 6 hex** and all duplicates
  regenerated with unique values (GRAPH=`3c4d1e`, ATTN=`9d8e7f`,
  TREE=`5e6f2a`, DOCSTUB=`6c5b4a`). CAP_PAIR grammar updated to require
  6 hex and allow `-` in capability names.
- **LEARNED-BPE canonical expansion before memory storage**.
- **Anaphora reserved from EMBED namespace**.
- **ATTN weighted** — EDIT=3, INTERACT=2, RATIONALE/REASONING=1.
- **ATTN decay** — 50%/turn, `[new task]` marker, session-id change
  triggers reset.
- **ATTN overflow priority** — weight-descending, most-recent tiebreak.
- **LEARNED-BPE coverage-based K** — cumulative 40% identifier
  frequency, cap 50, floor 5.
- **B-prefix reserved** in minification namespace.
- **DOCSTUB extended** to module-level and class-level docstrings.
- **EMBED top-K naming** — `as embed:e1,embed:e2`.

Seven review cycles; every priority list continues to be closed out
completely.

---

## New v3.0 capabilities — assessment

**STRUCTURED-CONF** — per-aspect confidence maps
(`{return_type=0.95, error_handling=0.4}`). Replaces scalar where
multi-facet uncertainty is real. Orchestrator uses min-aspect as
review gate. Backward-compat explicit (old parsers treat as absent).

**FORMAL-CLAIM** — SMT-LIB / Dafny / Z3 / Lean 4 code blocks for
machine-verifiable invariants. Shim dispatches to `verifier_registry`.
Status: `verified | refuted | unknown | no_verifier`. Refutations
return to model as correction feedback.

**POLYGLOT** — cross-language identifier mapping
(`UserAccount: go=UserAccount, python=user_account, ts=UserAccount`).
Shim validates against CodeIndex; persists via `remember:workspace`.

**COLLAB** — per-position gradient weights for multi-LoRA training
pipelines. `§6: weight=0.9` = "high-quality example, up-weight in
fine-tune."

All four are well-motivated. FORMAL-CLAIM is the most consequential —
it's the first protocol primitive with exact verification rather than
heuristic checking. POLYGLOT fills a real gap for microservice
architectures. COLLAB is niche but cleanly scoped.

---

## Blocking issues in v3.0

### B1. `CONF_ASPECT` grammar is ambiguous

```
CONF_ASPECT = PROSE , "=" , CONF_VAL ;
CONF_MAP    = "{" , CONF_ASPECT , { "," , " " , CONF_ASPECT } , "}" ;
```

`PROSE = { ? any char except newline ? }` — which includes `=`, `,`,
and `}`. A confidence map like `{a=0.5, b,c=0.9}` is syntactically
ambiguous:

- Is the first aspect name `a` with value `0.5`?
- Is `b,c` a malformed aspect name or two aspects?
- Does `}` inside a PROSE aspect name close the map early?

**Fix:** restrict aspect names to a constrained charset —

```
ASPECT_NAME = LCASE , { LCASE_ALNUM | "_" } ;
CONF_ASPECT = ASPECT_NAME , "=" , CONF_VAL ;
```

Consistent with `PERSONA_ID` / `EMBED_ID` / `MEM_SCOPE` patterns. Also
makes aspect names round-trippable for telemetry bucketing.

### B2. `POLYGLOT_MAP` grammar is just PROSE

```
POLYGLOT_MAP = "  " , PROSE , NEWLINE ;
POLYGLOT     = "polyglot:" , NEWLINE , POLYGLOT_MAP , { POLYGLOT_MAP } ;
```

§POLYGLOT specifies `  <canonical>: <lang>=<name>, <lang2>=<name2>`
but the grammar enforces none of this. A model emitting random prose
inside the `polyglot:` block passes grammar-level validation. This
defeats the purpose of a formal grammar.

**Fix:** structured EBNF for the entry form —

```
POLY_BINDING = LCASE , { LCASE_ALNUM } , "=" , PROSE_NOSPACE_NOCOMMA ;
POLY_LINE    = "  " , POLY_NAME , ": " , POLY_BINDING ,
               { ", " , POLY_BINDING } , NEWLINE ;
POLYGLOT     = "polyglot:" , NEWLINE , POLY_LINE , { POLY_LINE } ;
```

Without structure, constrained decoders can't enforce POLYGLOT output,
and the shim's "validates emitted polyglot maps against the CodeIndex"
promise can't rely on grammar to have produced well-formed entries.

---

## Medium issues

### M1. L2 model example in registry still has 4-hex fingerprint

```python
"qwen3.6-ozma-l2-v1": {
    "level": 2,
    "caps": {"IR": "ab12"},   # 4 hex — not updated to 6 hex
    ...
}
```

The L3 example was migrated to 6 hex; the L2 example was missed. Also
inconsistent with the `IR` fingerprint in the L3 block
(`"IR": "ab12cd"`). Trivial but visible.

### M2. FORMAL-CLAIM counterexample format unspecified

"Refuted claims are returned to the model as feedback in the next user
turn, similar to scope-check failures." — but *what format*?

- SMT-LIB `(model ...)` block?
- Natural-language translation of the model?
- The solver's raw stderr?
- A structured JSON?

The correction loop for FORMAL-CLAIM needs a concrete contract.
Without it, the LoRA can't be trained to respond to refutations
consistently across verifiers (SMT-LIB `(model)` ≠ Dafny
counterexample ≠ Lean 4 goal).

**Fix:** define a canonical normative form —

```
[formal-claim §6 refuted]
  counterexample: path="/dev/null", result=ERROR
  trace: 1. precondition holds; 2. postcondition violated: expected map[] got ERROR
```

Include the verifier-specific raw form as an optional trailer.

### M3. FORMAL-CLAIM language detection is by fenced-tag only

"Detects the formal language from the fenced code-block tag." But what
if the tag is missing, non-standard (`smt` vs `smt2` vs `smtlib`), or
the content doesn't match the tag? Behavior undefined.

**Fix:** enumerate canonical tags (`smt2`, `dafny`, `lean4`, `z3`);
missing/unknown → `no_verifier` with `tag_missing` sub-kind; accept
only those exact tags in normative mode.

### M4. Verifier registry not shown in registry example

The spec mentions `verifier_registry` in the deployment config but the
§Capability Registry example doesn't show it. Deployments using
FORMAL-CLAIM need concrete configuration:

```python
"verifier_registry": {
    "smt2":  "z3",
    "dafny": "dafny",
    "lean4": "lean",
}
```

Not in the example. Adds mystery to an otherwise discoverability-
friendly registry.

### M5. STRUCTURED-CONF minimum-aspect gate is fragile

The orchestrator uses `min(aspects)` as the review gate. A model
emitting `confidence={return_type=0.95, error_handling=0.9, style=0.3}`
triggers review because of `style=0.3`, even though the edit is
functionally correct. Low-quality or irrelevant aspects poison
high-quality edits.

**Fix:** either

- Aspects carry weights: `confidence={return_type=0.95[w=2], style=0.3[w=0.1]}` → weighted-min.
- Distinguish `primary` from `advisory`: only primary counts for the gate.

Either adds grammar complexity; pick one and commit.

### M6. POLYGLOT conflict resolution not specified

If turn 3 emits `UserAccount: go=UserAccount, python=user_account` and
turn 7 emits `UserAccount: go=UserAcct, python=user_acct`, what
happens?

- Latest-wins? (matches MEMORY's rule — sensible)
- Stored as alternatives?
- Requires explicit supersede?

**Fix:** adopt MEMORY's conflict-resolution semantics (newest-wins,
`[superseded]` tag, `merge_all` option). Uniformity across the
protocol is worth more than optimal per-type rules.

### M7. COLLAB weights are unvalidatable — trust-only

"gradient weight hints for the training pipeline" — but the model is
writing its own training weights, which is a conflict of interest. A
model rewarded for high weights tends to emit high weights. Telemetry
can catch calibration drift over time but nothing at turn-time stops
self-inflation.

**Fix:** document that COLLAB weights are advisory and should be
multiplied by an independent calibration factor (e.g., actual claim-
verification rate for that model). Or: restrict COLLAB to human-in-
the-loop workflows where human review dominates the weight.

---

## Smaller issues

### S1. `PERSONA_ID` max 32 chars still not enforced in grammar

```
PERSONA_ID = LCASE , { LCASE_ALNUM } ;   (* starts with letter, max 32 chars *)
```

Comment says "max 32 chars"; the EBNF `{ LCASE_ALNUM }` is unbounded.
Either add a length constraint (EBNF pragma or explicit repetition
bound) or enforce in the shim and document the grammar/implementation
gap.

### S2. COLLAB weight scalar only — asymmetric with STRUCTURED-CONF

```
COLLAB_ENTRY = "  " , POSITION , ": weight=" , CONF_VAL , NEWLINE ;
```

Uses `CONF_VAL` (scalar). But v3.0 introduced `CONF_MAP` for exactly
this case — "the weight on this position's correctness varies by
aspect." Consider making COLLAB weights accept both.

### S3. Profile table phrasing inconsistent

```
| `research` | All v3.0 caps | 70B+ | FORMAL-CLAIM, POLYGLOT, COLLAB |
```

Lists three caps but says "All v3.0 caps" — STRUCTURED-CONF is v3.0
too. Either include it or rephrase ("Advanced v3.0 caps").

### S4. FORMAL-CLAIM position binding is implicit

```
FORMAL_CLAIM = "formal-claim " , POSITION , ":" , NEWLINE , CODE_BLOCK ;
```

The SMT-LIB block doesn't reference `§6` internally — it's an
independent formalization. So `§6` is just metadata: "this claim is
about §6." What if the formalization and the position disagree (SMT
talks about `validate` but `§6` is `load`)? Shim can't detect.
Acknowledge explicitly as "trust the model to pair claims with
positions; mismatch is training signal."

### S5. POLYGLOT is "Bidirectional at L3" but input format isn't shown

The Data Types convention is input-format, output-format, L1-fallback
per type. POLYGLOT shows only output format. When the shim *injects* a
polyglot map (read side), what does the model see? Same format? A
`[polyglot map]` prefix block? Not specified.

### S6. `POLY_NAME` overlap with protocol keywords

A polyglot canonical name could be `remember` or `edit` or `session`.
If PROSE matching is lax, this works. But downstream parsers that
pattern-match on line prefix may get confused. Consider reserving
protocol keyword prefixes from POLY_NAME.

---

## Still missing — genuinely nothing left

Every item from `protocol-improvements.md` and all prior review lists
is now incorporated. The v3.0 additions go *beyond* those lists.

Forward-looking for v4 (none urgent):

- **Session cross-model identity** — if Alice's session uses model A
  for turns 1–5 and model B for turn 6, do session-cache chunks remain
  valid? Likely yes, but the protocol doesn't state it.
- **Federated learning hooks** — COLLAB is a step; explicit federated
  aggregation annotations would be next.
- **Differential privacy markers** — for multi-tenant training data
  aggregation, per-turn DP budget tracking.
- **Semantic versioning of the grammar itself** — per-cap fingerprint
  exists, but no top-level grammar-version covering shared primitives.

---

## Priority for v3.1

| Priority | Fix | Why |
|----------|-----|-----|
| P0 | Constrain `CONF_ASPECT` name charset (no `,` `=` `}`) | Grammar ambiguity in STRUCTURED-CONF |
| P0 | Structured EBNF for `POLYGLOT_MAP` (name + bindings) | Grammar currently enforces nothing |
| P1 | Normative counterexample format for FORMAL-CLAIM | Correction-loop training can't work without it |
| P1 | Enumerate canonical FORMAL-CLAIM language tags + missing-tag behavior | Prevent silent `no_verifier` drift |
| P1 | Verifier registry in the registry example | Discoverability |
| P1 | L2 registry example: 6-hex fingerprint | Consistency |
| P2 | STRUCTURED-CONF: primary/advisory or weighted aggregation | Prevent aspect-noise edit suppression |
| P2 | POLYGLOT conflict resolution rule | Specify newest-wins by default |
| P2 | COLLAB: documented advisory nature, calibration factor | Prevent weight self-inflation |
| P2 | `PERSONA_ID` length constraint in grammar | Spec/impl gap |
| P3 | COLLAB weight accept CONF_MAP | Symmetry with STRUCTURED-CONF |
| P3 | Profile table phrasing ("All v3.0 caps" excludes STRUCTURED-CONF) | Accuracy |
| P3 | FORMAL-CLAIM position-vs-formalization mismatch policy | Training-signal clarity |
| P3 | POLYGLOT input-format specification | Data Types convention completeness |
| P3 | `POLY_NAME` reserved-keyword policy | Parser robustness |

---

## Overall

v3.0 is effectively v1.0-final with four advanced capabilities on top.
The blocking issues (CONF_ASPECT ambiguity, POLYGLOT grammar laxity)
are small EBNF fixes — a few hours of work. The verification-loop
details for FORMAL-CLAIM are the biggest real gap; they need concrete
definition before the capability can ship usefully.

v1.0 → v3.0 across seven revision cycles. Every prior review's
priority list closed out. New capabilities added at the same rigor
level as the base protocol. Grammar still CI-verified for uniqueness.
The protocol is production-quality as a standard document. Remaining
work is implementation detail and small polish.
