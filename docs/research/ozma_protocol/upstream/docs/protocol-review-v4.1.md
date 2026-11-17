# Ozma Protocol v4.1 — Review

**Reviewer:** Claude (Opus 4.7)
**Date:** 2026-04-19
**Target:** `protocol.md` v4.1 (2026-04-19, 3359 lines)
**Priors:** `protocol-review-v4.0.md` (Sonnet review that shipped v4.1)

v4.1 is a clean execution of the Sonnet v4.0 review — every flagged
item addressed, no new bugs introduced by the fixes. Several items
from my earlier v4.0 analysis (replaced by the Sonnet version) remain
unaddressed and are flagged as residuals.

---

## What landed in v4.1

- **`DP_FLOAT` introduced** — accepts `0.1`, `1e-5`, `2.0`, `10`. The
  §DP example now parses.
- **`LCASE` / `LCASE_ALNUM` moved** to shared primitives section —
  forward-reference problem for parser generators fixed.
- **`LCASE_WORD` added** for hyphen-free names. `ASPECT_NAME`,
  `POLY_NAME`, `POLY_LANG` now use `LCASE_WORD`; grammar matches prose.
- **`FORMAL_CLAIM` moved after `BODY` in `TURN`** — claim-after-edit
  semantic order; matches training example 13.
- **Duplicate `LCASE`/`LCASE_ALNUM` removal** from PERSONA section.
- **Training example 11 lowercase `b1`/`b2`** — matches LEARNED-BPE
  namespace reservation.
- **FORMAL-CLAIM `tag_missing` vs `tag_unknown`** distinguished.
- **v4 telemetry fields** — `federated_positions`, `dp_epsilon`,
  `dp_delta`, `dp_composition`, `model_switch_cap_delta`,
  `formal_verify_advisory_aspects_excluded`.
- **Training examples 1–3 ack bumped** to v4.0.
- **Profile table `federated`** clarified to "all caps through v4.0".
- **New v4 headers** — `X-Ozma-DP-Epsilon-Cap`, `X-Ozma-DP-Delta-Cap`,
  `X-Ozma-Federated-Round`.
- **`POLY_ID` requires at least one character**.
- **Grammar: 88 unique productions, CI-verified.**

Every Sonnet review item addressed cleanly.

---

## Residual issues from the prior v4.0 analysis (not in Sonnet review)

### R1. Cross-model identity has no re-auth — anti-spoof guarantee lapses

§Cross-model identity within a session: "Ack is once-per-session. The
new model does NOT re-emit an ack header."

But ack carries the HMAC proving the model is the trained model. If
model B never acks, the shim never verifies model B's identity — a
compromised orchestrator can swap in any model mid-session. The anti-
spoof defense established in v1.6 doesn't extend past the first-ack'd
model.

**Fix:** require a silent probe/ack exchange on model switch (not
visible in the conversation but validates model B's HMAC). Or document
explicitly that the anti-spoof guarantee is first-model-only.

### R2. FEDERATED `agg_weight` scalar-only — asymmetric with COLLAB

```
FED_ENTRY = "  " , POSITION , ": include=" , ( "true" | "false" ) ,
            [ " round=" , ORDINAL ] ,
            [ " agg_weight=" , CONF_VAL ] , NEWLINE ;
```

COLLAB accepts `CONF_VAL | CONF_MAP` for weight. FEDERATED `agg_weight`
is scalar only. Per-aspect federated weighting can't be expressed.

**Fix:** `agg_weight=" , ( CONF_VAL | CONF_MAP )`.

### R3. `GRAMMAR_VER` production is still present but never emitted

```
GRAMMAR_VER = "grammar-version: " , ORDINAL , "." , ORDINAL , NEWLINE ;
```

§GRAMMAR says "the GRAMMAR_VER production exists only to formally
define the concept" and "the model never emits a grammar-version:
marker at runtime."

A grammar production defined purely for conceptual completeness
clutters the EBNF. The fingerprint in the ack (`GRAMMAR:789abc`) is
the actual mechanism.

**Fix:** either use it (emit once in a `[grammar context]` prefix) or
remove it and describe in prose.

### R4. GRAMMAR fingerprint-mismatch fallback still too narrow

"Falls back to treating all structured-confidence output as scalar-
confidence for that session."

Shared primitives are used by many capabilities — POLYGLOT canonical
names, COLLAB CONF_MAP, FEDERATED CONF_VAL, DP DP_FLOAT. Only
STRUCTURED-CONF has a documented fallback; other caps depending on
mismatched primitives have no specified behavior.

**Fix:** generalize — "falls back any capability whose grammar depends
on the mismatched primitive to its safest interpretation" — and
publish a capability → shared-primitive dependency map.

### R5. FEDERATED round inference partially addressed

`X-Ozma-Federated-Round: N` header lets the orchestrator override the
current round. But the *default* inference rule (when no header is
present and the model omits `round`) remains unspecified. Monotonic
increment? Announced by aggregator? Implementations will diverge.

### R6. DP accounting ownership still implicit

"The training pipeline aggregates per-turn epsilon/delta values using
the specified composition method" — the shim emits the markers; who
owns the accountant? Out-of-scope statement would be better than
vague.

### R7. DP identity rotation risk

Per-user DP budget tracking via `user_id = sha256(api_key)[:16]`
resets on key rotation → privacy violation for previously-committed
budget. DP tracking should require OAuth-derived identity or
explicitly accept rotation-reset as a known limitation.

---

## Smaller residuals

- **DP_PARAM order** still fixed (`epsilon` before `delta`). Order-
  independence or explicit documentation.
- **FEDERATED `include=false` vs. absence** still not disambiguated.
- **COLLAB `CONF_MAP` weight aggregation semantics** still unspecified.
- **POLY_NAME reserved list** still missing `immediate`, `preview`,
  `add`, `delete`, `session`, `workspace`, `user`, and language tags.
- **Cross-model capability intersection** — persistent state note
  still not explicit (memories written by model A persist but cap B
  may not be able to `recall`).

---

## New v4.1 observations

- **`X-Ozma-DP-Epsilon-Cap: 1.0`** default. DP_FLOAT allows unbounded
  epsilons (realistic ε=2, ε=10). Deployments with realistic DP will
  need to override the default explicitly. Minor.
- **`X-Ozma-DP-Delta-Cap: 1e-5`** — implicitly requires the header
  parser to accept scientific notation. §Extension Points table
  doesn't formalize header-value parsing rules; consistent with
  DP_FLOAT in practice.
- **`X-Ozma-Federated-Round: N`** — good addition for deterministic
  round targeting.
- **Grammar at 88 productions**, up from 86 (two new — `DP_FLOAT` and
  `LCASE_WORD`). CI invariant holds.

---

## Priority for v4.2

| Priority | Fix | Why |
|----------|-----|-----|
| P1 | Cross-model identity re-auth | Anti-spoof guarantee gap |
| P1 | FEDERATED `agg_weight` accepts `CONF_MAP` | Symmetry with COLLAB |
| P1 | Remove or operationalize `GRAMMAR_VER` production | Unused grammar |
| P1 | GRAMMAR fingerprint-mismatch fallback: cover all shared-primitive caps | Current fallback only mentions STRUCTURED-CONF |
| P2 | FEDERATED default round inference rule | Prevent implementation divergence |
| P2 | DP accounting ownership (spec boundary) | Clarity |
| P2 | DP identity rotation policy | Privacy-violation risk |
| P2 | DP_PARAM order-independence or explicit documentation | Fragility |
| P2 | FEDERATED `include=false` vs. absence semantics | Ambiguity |
| P2 | COLLAB `CONF_MAP` weight aggregation rule | Pipeline consistency |
| P3 | POLY_NAME reserved list expansion | Completeness |
| P3 | Persistent-state note on cross-model cap switches | Observability |

---

## Overall

v4.1 cleanly executes the Sonnet v4.0 review. Residual issues are
meaningful (cross-model auth is a real security gap; `GRAMMAR_VER`
is awkward grammar; FEDERATED/COLLAB asymmetry is simple
inconsistency) but none are blockers for shipping. All fit a v4.2
polish release.

Ten revision cycles; follow-through remains excellent.
