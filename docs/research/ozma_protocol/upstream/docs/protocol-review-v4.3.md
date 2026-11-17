# Ozma Protocol v4.3 — Review

**Reviewer:** Claude (Opus 4.7)
**Date:** 2026-04-19
**Target:** `protocol.md` v4.3 (2026-04-19, 3507 lines)
**Priors:** `protocol-review-v4.2.md`

v4.3 addresses every v4.2 review item *and* independently catches 8
grammar-hardening issues missed in prior reviews. Pure hardening
release — no new capabilities, one production added (HMAC). This is
the cleanest revision cycle.

---

## What landed in v4.3

### From the v4.2 review

- **`strict_antispoof: false` registry flag** — setting to `true`
  makes the silent probe/ack on model switch a MUST with shim
  enforcement. `silent_ack_required` logged on refused turns. Closes
  the SHOULD gap cleanly.
- **CONF_VAL/CONF_MAP grammar-mismatch fallback changed to
  `confidence=0.5`** — now fails toward the review gate, not toward
  auto-apply.
- **`dp_user_id` configuration mechanism specified** — registry key
  (null = derive from api_key hash) plus `X-Ozma-DP-User-Id`
  per-request header.
- **Changelog `add` typo fixed**.
- **POLY_NAME reserved list normalization note added** —
  `formal-claim` → `formal_claim` via hyphen→underscore rule.

### Independently identified grammar fixes

- **HASH_REF/CHUNK_HDR duplicate elimination** — Session Cache
  subsection fence tag changed from `ebnf` to generic prose; header
  renamed "Grammar (illustration — normative definition in §Formal
  Grammar)". CI invariant preserved without losing the illustrative
  snippet.
- **`FILE` one-or-more** — empty filename now rejected.
- **`HINT_LABEL` production added** — HINT_LINE PROSE-absorbs-CONF_VAL
  ambiguity now documented.
- **`DP_FLOAT` alternatives reordered most-specific first** — CFG
  ambiguity note added ("PEG: ordered choice").
- **`PLAN_STEP` simplified to PROSE-only** — INT_CMD/EDIT_CMD variants
  removed (no training data used them, double-NEWLINE issue).
- **`EDGE` parsing note** — PROSE-before-separator domain constraint
  documented.
- **`STABLE_ID` lowercase-only** — comment added (sha256 hex output
  is lowercase).
- **`HMAC` production added** — 8 lowercase hex digits; `ACK` no
  longer uses unbounded HEX.
- **`LCASE` / `LCASE_ALNUM` location note** — placed in shared
  primitives region explicitly for forward-reference clarity.

Grammar: 89 unique productions, CI-verified.

All v4.2 review items cleanly addressed, plus real hardening missed
in prior reviews. The v4.3 fixes are exactly the kind of CFG-
ambiguity issues that would bite real parser-generator deployments.

---

## New issues in v4.3

### N1. Grammar is now implicitly PEG, not strict CFG

Several v4.3 fixes document known ambiguities without removing them:

- `DP_FLOAT` alternatives ordered "most-specific first. CFG parsers
  must not commit to a shorter prefix when a longer one matches. PEG:
  ordered choice."
- `HINT_LABEL = PROSE` with the constraint "no embedded ` <CONF_VAL>
  (` pattern".
- `EDGE` PROSE labels "do not contain ` --> `".

These are legitimate constraints for PEG / longest-match / greedy-
terminator parsers but aren't expressible in strict CFG. A generator-
agnostic tool (e.g., a pure LALR parser) would need additional logic.

**Fix:** add a normative paragraph at the top of §Formal Grammar
stating that the grammar is PEG-parseable / ordered-choice, not
strict CFG. This guides parser-generator choice (Lark in Earley mode,
llguidance, xgrammar all work; pure LALR(1) needs additional logic).
Without the note, implementers may choose a generator that can't
honor the ordered-choice semantics and produce parsers that silently
mis-parse at edges.

### N2. PLAN_STEP capability regression worth tracking

```
PLAN_STEP = "  p" , ORDINAL , [ CONF_ATTR ] , ": " , PROSE , NEWLINE ;
```

Simplified from `( INT_CMD | EDIT_CMD | PROSE )` to `PROSE` only.
Rationale: "no training data used them, double-NEWLINE issue."

Cleanup of an unused production is fine. But plan steps are now
opaque prose; the orchestrator cannot execute structured plan steps
programmatically (requires NLU). If autonomous agent workflows later
need "execute this step as an INT_CMD," the grammar has to be
re-widened.

Not a bug; a tracked regression.

**Fix:** add a comment to PLAN_STEP noting the design intent ("plan
steps are human-readable only; execution happens via BODY, never via
plan steps") so future maintainers understand the simplification was
deliberate.

### N3. Session Cache "Grammar" subsection might mislead readers

```
### Grammar (illustration — normative definition in §Formal Grammar)
```

A reader scanning TOC or section headers sees "Grammar" and might
treat the snippet as authoritative. The inline header clarifies, but
TOC-level navigation doesn't.

**Fix:** rename to `### Grammar (illustrative)` or `### Overview` so
the TOC itself signals non-normative status. Tiny change.

### N4. `strict_antispoof` needs telemetry fields

The shim "MUST complete a silent probe/ack exchange" when the flag is
true; refused turns log `silent_ack_required`. But the telemetry
event doesn't include observability for this. A deployment running
`strict_antispoof: true` has no visibility into how often silent
probe/ack is triggered or how long it takes.

**Fix:** add `strict_antispoof_active: bool` and
`silent_ack_ms: int|null` to the telemetry schema.

---

## Still open — nothing substantive

The forward-looking v5 directions from prior reviews remain as future
work but are not blockers:

- Capability-level ACL.
- Multi-turn formal-claim accumulation.
- Session-external retrieval.
- Cross-model drift observability dashboards.

---

## Priority for v4.4

| Priority | Fix | Why |
|----------|-----|-----|
| P2 | Formal grammar preamble: PEG / ordered-choice semantics | Guides parser-generator choice; prevents silent mis-parses on CFG tooling |
| P3 | PLAN_STEP design-intent comment | Prevents future maintainers re-widening without understanding |
| P3 | Rename Session Cache "### Grammar (illustration ...)" in TOC | Navigation clarity |
| P3 | Telemetry fields for `strict_antispoof` observability | Deployment visibility |

---

## Overall

v4.3 is the tightest revision yet — every v4.2 review item addressed
plus 8 independently-caught grammar issues that would bite real
deployments. The grammar is now parseable by PEG tooling cleanly,
with ambiguities explicitly documented rather than silently present.
Only nit: the ambiguity documentation implicitly constrains parser
choice; worth stating the PEG assumption explicitly.

Twelve revision cycles. The protocol is past shippable and into
"polish forever" territory. Genuine convergence.
