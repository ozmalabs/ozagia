# Ozma Protocol v4.4 — Review

**Reviewer:** Claude (Sonnet 4.6)
**Date:** 2026-04-19
**Target:** `protocol.md` v4.4 (2026-04-19, ~3520 lines)
**Priors:** `protocol-review-v4.3.md`

v4.4 addresses every v4.3 review item cleanly. Pure polish release —
no new capabilities, no grammar additions (89 productions maintained).
All four N-items from the v4.3 review are resolved.

---

## What landed in v4.4

### From the v4.3 review

- **Formal grammar PEG preamble added** — normative paragraph at top of
  §Formal Grammar states the grammar is PEG / ordered-choice, not strict
  CFG. Names compatible tooling (Lark/Earley, llguidance, xgrammar) and
  explains LALR(1) limitation. Closes N1.
- **PLAN_STEP design-intent comment added** — multi-line comment above
  PLAN_STEP production explains the simplification was deliberate: plan
  steps are human-readable only; execution via BODY; widening requires
  new training data. Closes N2.
- **Session cache Grammar subsection renamed** — "### Grammar
  (illustration — normative definition in §Formal Grammar)" →
  "### Grammar (illustrative)". TOC now signals non-normative status
  without needing to read the inline header text. Closes N3.
- **Telemetry schema extended** — `strict_antispoof_active: bool` and
  `silent_ack_ms: int|null` added to the telemetry event. Deployments
  running `strict_antispoof: true` now have observability for how often
  silent probe/ack fires and how long it takes. Closes N4.

Grammar: 89 unique productions, CI-verified. No changes.

All v4.3 review items cleanly addressed.

---

## New issues in v4.4

### N1. Telemetry fields lack documentation in the prose below the schema

The schema comment block at the bottom of §Telemetry Schema explains
several fields:

> `null` fields indicate the capability was not exercised this turn.
> `persona_change_requested` is true when... `tokenizer` is...
> `upstream_prompt_cache_tokens` counts... `hardware_class` enables...

The new `strict_antispoof_active` and `silent_ack_ms` fields are not
explained in this prose. A reader needs to know:
- `strict_antispoof_active: false` means the registry flag is `false`
  this turn, not that the exchange failed.
- `silent_ack_ms: null` means no exchange occurred (flag was false or
  turn was not a model-switch turn).

**Fix:** add one sentence to the telemetry prose: "`strict_antispoof_active`
mirrors the registry flag; `silent_ack_ms` is the probe/ack round-trip
latency in milliseconds, null when no exchange occurred this turn."

### N2. PEG preamble lists `TS_LINE` in the body but not in the header sentence

The preamble says (paraphrasing): "…specifically `DP_FLOAT`, `HINT_LINE`,
and `EDGE` / `TS_LINE`…" — `TS_LINE` appears in the list but `EDGE` is
the primary example in the prose. This is fine for `EDGE` since the
EDGE production has an explicit domain-constraint comment. But `TS_LINE`
has a similar constraint (PROSE label does not contain ` → `) that is
documented the same way. No action needed — just confirming symmetry.

Actually: if both `EDGE` and `TS_LINE` have inline comments, the preamble
should mention both explicitly and uniformly. Currently the preamble lists
`EDGE / TS_LINE` as a pair, which is correct. No issue.

### N3. `silent_ack_ms` type annotation is informal

The telemetry JSON shows `"silent_ack_ms": null`. The review requested
`int|null`. JSON has no type annotation syntax, so the type is implicit.
However, the prose explanation added in N1 above should specify the unit
(milliseconds) and type (integer, not float) explicitly, since sub-
millisecond timing could be reported as a float by some runtimes.

**Fix:** in the prose note (from N1), specify "integer milliseconds
(rounded up)."

---

## Still open — nothing substantive

Forward-looking v5 directions remain as future work and are not blockers:

- Capability-level ACL.
- Multi-turn formal-claim accumulation.
- Session-external retrieval.
- Cross-model drift observability dashboards.

---

## Priority for v4.5

| Priority | Fix | Why |
|----------|-----|-----|
| P3 | Telemetry prose: document `strict_antispoof_active` and `silent_ack_ms` semantics | Reader clarity for new fields |
| P3 | Telemetry prose: specify `silent_ack_ms` as integer milliseconds (rounded up) | Type precision for monitoring integrations |

---

## Overall

v4.4 is a clean close of the v4.3 review. All four items addressed without
introducing new issues of substance. The two new items are both P3 prose
clarifications for the telemetry fields just added — a natural consequence
of adding new fields without documentation prose.

Thirteen revision cycles. The protocol is in genuine polish territory.
The grammar is stable, the capabilities are complete, and the remaining
work is documentation precision rather than specification correctness.
