# Ozma Protocol v5.1 — Review

**Reviewer:** Claude (Opus 4.7)
**Date:** 2026-04-19
**Target:** `protocol.md` v5.1 (2026-04-19, 4196 lines)
**Priors:** `protocol-review-v5.0-FINAL_final(1)_v2.md`

v5.1 is a cleanup release that addresses all P0 and P1 items from the
v5.0 review, plus the easy P2s. Three real new issues surface — one
grammar regression (EFFECT_HOST now rejects valid hostnames), one
count-bookkeeping repeat (production count still wrong by a different
amount), and one missed fingerprint cascade.

---

## What landed

### All P0 fixes

- **Grammar production count** corrected in changelog (107 → 123
  claimed; see B1 below for the new miscount).
- **AGENT-STATE fingerprint** regenerated `0c1d2f` → `a3b4c5` — no
  longer conflicts with MATH.
- **`DEP_LINE` whitespace** made conditional via new `DEP_DETAIL` and
  `DEP_NOTE` both optional. `dep foo: audit` now parses cleanly.
- **`EFFECT_VAL`** PROSE hostname alternative replaced with dedicated
  `EFFECT_HOST = LCASE_WORD , { "." , LCASE_WORD }` production.
- **`PROMPT_BLOCK` tag enforcement** via new `PROMPT_TAG = "text" |
  "prompt"` — untagged code blocks no longer silently accepted.

### All P1 fixes

- **`SCHEMA_LINE` parsing note** added via `SCHEMA_DETAIL`
  intermediate production with PEG-lookahead comment.
- **`pure=true` contradiction rule** documented — `effects_contradiction`
  logged; offending line dropped.
- **MATH block terminator** rule documented (first non-2-space-indent
  line).
- **AGENT-STATE persistence scope** specified — `workspace` default,
  `agent_state_scope` registry key (values: `session` | `workspace` |
  `user`), keyed by `workspace_id + sha256(goal)[:8]`.
- **`AGENT_STATE_DELTA` production** for progress-only updates.
- **8 new error kinds** enumerated: `shape_violation`,
  `schema_compat_mismatch`, `schema_breaking`,
  `schema_format_illegal_op`, `dep_cve_unresolved`,
  `effects_contradiction`, `prompt_injection_risk`,
  `prompt_token_budget_exceeded`.

### P2 cleanups

- **`PROGRESS_VAL = CONF_VAL`** alias in AGENT-STATE for semantic
  readability.
- **SCHEMA + EDIT interaction rule** documented (SCHEMA describes
  intent; EDIT applies; shim cross-checks delta).
- **Capability profile names** hyphenated consistently (`full-v2`,
  `research-v3`, `federated-v4`, `ml-code-v5`, `api-code-v5`,
  `agent-v5`, `full-v5`).
- `TURN` production now `[ AGENT_STATE | AGENT_DELTA ]`.

Every priority item from the prior review cleanly addressed.

---

## New issues introduced in v5.1

### N1. `EFFECT_HOST` rejects valid hostnames — grammar regression

```
LCASE_WORD  = LCASE | DIGIT | "_" ;   (* [a-z0-9_] — no hyphen *)
EFFECT_HOST = LCASE_WORD , { "." , LCASE_WORD } ;   (* e.g. "api.example.com" *)
```

`LCASE_WORD` excludes hyphens (correct for `ASPECT_NAME`, `POLY_NAME`,
`POLY_LANG`, where hyphens were intentionally disallowed). But real
hostnames contain hyphens: `api-gateway.example.com`,
`my-service.internal`. `EFFECT_HOST` rejects them.

v5.0's `PROSE` alternative accepted hyphens (by accepting everything).
v5.1's `EFFECT_HOST` over-restricted and introduced a regression.

**Fix:** introduce a dedicated `HOST_LABEL` production that allows
hyphens per RFC 1123:

```
HOST_LABEL  = LCASE , { LCASE | DIGIT | "-" } ;   (* [a-z][a-z0-9-]* *)
EFFECT_HOST = HOST_LABEL , { "." , HOST_LABEL } ;
```

This preserves whitespace-freedom (so no overflow hazard) while
accepting real hostnames. Hyphens at label boundaries (leading/
trailing) are invalid per RFC 1123 but not enforced by this grammar;
acceptable for the protocol's purposes.

### N2. Grammar production count still wrong

Changelog says: "Grammar: 125 unique productions (123 + PROGRESS_VAL +
EFFECT_HOST + AGENT_STATE_DELTA refactor)."

Three new productions listed, but v5.1 actually added seven:

- `PROGRESS_VAL` (new alias)
- `EFFECT_HOST` (new)
- `AGENT_DELTA` (new)
- `SCHEMA_DETAIL` (new alias)
- `DEP_DETAIL` (new)
- `PROMPT_TAG` (new)
- `PROMPT_BLOCK` (new)

Starting from 123, actual count is **130**.

This is the same class of bookkeeping bug as v5.0 (claimed 107, actual
123). The CI invariant should catch this if the count is
machine-generated. If it's manually written in the changelog, the
changelog line should be replaced with a build-time injected count.

**Fix:** correct to 130. Consider generating the count via CI to
prevent recurrence.

### N3. Fingerprint cascade missed for grammar-changing caps

v5.1 changes the grammar for EFFECTS (added EFFECT_HOST), AGENT-STATE
(added AGENT_DELTA), PROMPT (added PROMPT_TAG / PROMPT_BLOCK), SCHEMA
(added SCHEMA_DETAIL alias), and DEP (added DEP_DETAIL). Per the
established `sha256(grammar_section)[:6]` rule, each of these caps
should receive a new fingerprint.

Registry example still shows:

```
"SCHEMA":           "8c9d0e",  # v5.0
"DEP":              "1f2a3b",  # v5.0
"EFFECTS":          "4c5d6e",  # v5.0
"PROMPT":           "7f8a9b",  # v5.0
"AGENT-STATE":      "a3b4c5",  # v5.0
```

These were computed against the v5.0 grammar sections. v5.1's grammar
changes should cascade to new fingerprints (say, `8c9d0f`, `1f2a3c`,
`4c5d6f`, `7f8a9c`, `a3b4c6`). Models trained against v5.0 grammar
then deployed against v5.1 shim will fail the fingerprint check — but
only if the shim is actually recomputing. If the registry is manually
maintained, the discrepancy goes unnoticed.

**Fix:** either (a) recompute fingerprints in CI and update the
registry example, or (b) document that v5.1 is grammar-backward-
compatible with v5.0 for existing cap shapes — which it is, since
the changes are additive (new optional productions that don't break
v5.0-valid output). If backward-compatible, the GRAMMAR cap
fingerprint changes but individual cap fingerprints don't need to.
Pick and document.

---

## Smaller issues

### S1. `AGENT_STATE_DELTA` grammar can't enforce "at least one field"

```
AGENT_DELTA = "agent-state-delta:" , NEWLINE ,
              [ AGENT_GOAL ] , [ AGENT_PROG ] ,
              [ AGENT_SUBS ] , [ AGENT_BLOCK ] ;
```

All four fields optional. Grammar accepts a bare `agent-state-delta:\n`
with no body. Comment says "At least one field MUST be present" — but
EBNF can't express the constraint. Shim must enforce.

Not a blocker; worth a parsing-note comment or a shim-side validation
line in §Shim Responsibilities.

### S2. DEP PEG parsing note missing

`SCHEMA_LINE` has a PEG-lookahead parsing note for the
PROSE-vs-COMPAT ambiguity. `DEP_LINE` has the same pattern
(`DEP_DETAIL` PROSE vs trailing `[DEP_NOTE]`) but no parallel note.

**Fix:** copy the SCHEMA_LINE note to DEP_LINE — "PEG: match DEP_NOTE
greedily from end-of-line, then DEP_DETAIL is what's left."

### S3. `effects_contradiction` behavior partially specified

"The shim rejects such lines with `effects_contradiction` logged; no
partial application occurs."

What does "no partial application" mean? Is the whole turn rejected?
Is just the offending EFFECTS line dropped? Most consistent with other
error kinds (drop-line-keep-rest) but not stated.

**Fix:** specify — "only the offending EFFECTS line is dropped; other
protocol output in the turn is unaffected."

### S4. New error kinds lack corresponding telemetry counters

`shape_violation`, `schema_compat_mismatch`, `prompt_injection_issues`
have telemetry fields. But `dep_cve_unresolved`,
`schema_format_illegal_op`, `effects_contradiction`,
`prompt_token_budget_exceeded` don't have dedicated counters — they'd
land in generic `check_regression_errors` or nowhere.

Minor; pattern-match the existing error-kind → telemetry-counter
convention.

### S5. PROMPT_BLOCK internal ``` handling inherited from CODE_BLOCK

`PROMPT_BLOCK = "```" , PROMPT_TAG , NEWLINE , { ? any line ? } ,
"```"` — "any line" includes lines starting with ``` which would
prematurely terminate the block. Convention (no nested triple-
backticks inside prompts) inherited from CODE_BLOCK. Fine but worth
noting if a prompt literally needs to contain triple backticks.

### S6. `agent_state_scope` registry key not in telemetry

New registry key added but no telemetry field reflects which scope is
active for a given session. Useful for observability across multi-
deployment fleets.

**Fix:** add `agent_state_scope: string` to the telemetry schema.

---

## Still open — unchanged from prior reviews

- Forward-looking v6 directions (capability-level ACL, multi-turn
  formal-claim accumulation, session-external retrieval, cross-model
  drift observability dashboards).
- `PLAN_STEP` is still PROSE-only (deliberate, per v4.3 comment).

Nothing regressed structurally.

---

## Priority for v5.2

| Priority | Fix | Why |
|----------|-----|-----|
| P0 | `HOST_LABEL` production for `EFFECT_HOST` to accept hyphenated hostnames | New grammar regression; real hostnames rejected |
| P1 | Correct grammar production count (130, not 125) and CI-generate it | Same class of bug as v5.0 |
| P1 | Decide fingerprint cascade policy (recompute per-cap, or document backward-compat) | Current ambiguity may hide real mismatches |
| P2 | DEP_LINE PEG parsing note (parallel to SCHEMA_LINE) | Consistency |
| P2 | `AGENT_STATE_DELTA` "at least one field" shim enforcement note | Grammar can't express the constraint |
| P2 | `effects_contradiction` behavior — drop-line-keep-rest | Specify consistency with other error kinds |
| P2 | Telemetry counters for missing v5 error kinds | Observability |
| P3 | `agent_state_scope` in telemetry schema | Fleet observability |
| P3 | PROMPT_BLOCK nested ``` note (inherited) | Clarity |

---

## Overall

v5.1 executes the v5.0 review precisely — every P0/P1 item closed out,
several P2s included. Three new issues surface: one real grammar bug
(`EFFECT_HOST` hyphen regression — a classic "fix introduced a new
edge case"), one recurring bookkeeping error (production count
wrong), and one fingerprint-cascade decision needed.

Only N1 (the hyphen regression) would bite implementers on day one.
The rest is bookkeeping and documentation.

Fourteen revision cycles. The protocol continues to converge; the
cycle frequency itself has not slowed, but the scope of each cycle's
fixes has. v5.2 with the three items above genuinely reaches
"production-ready" on the v5.x capability set.
