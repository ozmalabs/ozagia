# Ozma Protocol v4.2 — Review

**Reviewer:** Claude (Opus 4.7)
**Date:** 2026-04-19
**Target:** `protocol.md` v4.2 (2026-04-19, 3450 lines)
**Priors:** `protocol-review-v4.1.md`

v4.2 closes every P1/P2/P3 item from the v4.1 review. Pure polish
release — no new capabilities, no grammar additions, one removal
(`GRAMMAR_VER` production deleted, now 87 productions). This is the
tightest revision cycle so far.

---

## What landed in v4.2

- **Cross-model anti-spoof scope documented** — first-model-only; the
  shim cannot verify model B's identity on switch. Orchestrators
  SHOULD trigger an out-of-band silent probe/ack exchange if per-model
  authentication matters.
- **FEDERATED `agg_weight` accepts `CONF_VAL | CONF_MAP`** — symmetry
  with COLLAB achieved. Aggregation rule: min primary-aspect is the
  effective weight.
- **`GRAMMAR_VER` production removed**. The GRAMMAR capability is
  documented in prose only; fingerprint in ack is the sole
  machine-readable version identifier.
- **GRAMMAR fingerprint-mismatch fallback generalized** — a table
  mapping primitives → affected capabilities → fallback strategy.
  CONF_VAL/CONF_MAP mismatch treats all structured-confidence as
  `confidence=1.0`; POSITION family drops to L1; PROSE/CODE_BLOCK
  drops to L1.
- **FEDERATED round inference rule**: `max(seen) + 1`, starts at 1 if
  none seen; `X-Ozma-Federated-Round` overrides.
- **DP accounting ownership clarified** — shim emits markers, pipeline
  owns the accountant.
- **DP identity-rotation-reset** documented as known limitation;
  OAuth-derived identity required for continuity.
- **DP_PARAM order** documented as grammar-fixed (epsilon before delta).
- **FEDERATED `include=false` semantics** clarified — explicit
  exclusion; omitting an entry is equivalent; `include=false`
  preferred when overriding an earlier turn's `include=true`.
- **COLLAB `CONF_MAP` weight aggregation**: min primary-aspect is
  effective gradient weight (consistent with FEDERATED).
- **Cross-model persistent-state note** added: memories from model A
  persist; model B without MEMORY cap sees injected memories as plain
  text.
- **POLY_NAME reserved list** comprehensively expanded with protocol
  commands, memory scopes, and language tags.
- **Changelog order corrected** (oldest-last now).

Every v4.1 residual addressed cleanly.

---

## New issues in v4.2

### N1. Silent probe/ack on model switch is SHOULD, not MUST

```
Orchestrators requiring per-model authentication SHOULD trigger an
out-of-band silent probe/ack exchange before passing any turn to the
new model.
```

SHOULD is an improvement over the previous "no mention," but security-
sensitive deployments have no conformance hook. An operator that
doesn't implement it loses anti-spoof on switches silently.

**Fix:** `strict_antispoof: true` registry flag that upgrades SHOULD
to MUST with shim enforcement — turns refused until silent probe/ack
completes, `silent_ack_required` logged.

### N2. CONF_VAL/CONF_MAP fallback to `confidence=1.0` is liberal

```
| `CONF_VAL` / `ASPECT_NAME` / `CONF_MAP` | ... |
  Treat all structured-confidence output as scalar `confidence=1.0` |
```

Confidence=1.0 means "absent confidence = proceed without review." On
a grammar-version mismatch, this causes low-confidence edits to
auto-apply because the shim can't parse the structured form.

**Fix:** safer default `confidence=0.5` — triggers review gate by
default, matches the existing `X-Ozma-Confidence-Threshold` default,
and fails toward caution rather than action.

### N3. `dp_user_id` override is prose-only, not a documented mechanism

§DP says: "Deployments requiring continuity across key rotations MUST
use an OAuth-derived or otherwise stable identity and configure the
shim with an explicit `dp_user_id` override."

But `dp_user_id` isn't in the registry example or the header table.
How is it configured? Registry key? Header? Deployment config file?

**Fix:** add `dp_user_id` to the registry example (deployment-level
config) or define an `X-Ozma-DP-User-Id` header.

### N4. Typo in changelog

```
POLY_NAME reserved list expanded (`immediate`, `preview`, `add`,
`delete`, `user`, `add`, and all ...
```

`add` appears twice. Trivial.

### N5. Reserved-list notation inconsistency

The POLY_NAME reserved list includes `formal_claim` (underscore). The
actual command name is `formal-claim` (hyphen). POLY_NAME uses
`LCASE_WORD` (no hyphen), so the reserved token correctly matches the
no-hyphen form, but the documented command in prose is `formal-claim`.

**Fix:** note explaining the normalization ("hyphens in command names
are normalized to underscores for POLY_NAME reservation purposes").

---

## Still open — nothing substantive

Everything from the improvements doc, and every cross-review priority
list, is now addressed. Forward-looking v5 directions remain as future
work but are not blockers:

- Capability-level ACL (which users/roles can use which caps).
- Multi-turn formal-claim accumulation.
- Session-external retrieval (`search workspace` across files not in
  current CodeIndex).
- Cross-model drift observability (dashboards for
  `model_switch_cap_delta`).

---

## Priority for v4.3

| Priority | Fix | Why |
|----------|-----|-----|
| P2 | `strict_antispoof: true` registry flag for MUST enforcement | Close the SHOULD gap for security-sensitive deployments |
| P2 | CONF_MAP fallback value: `0.5` not `1.0` | Fail toward review, not toward action |
| P2 | `dp_user_id` configuration mechanism (registry key or header) | Currently prose-only |
| P3 | Fix `add` typo in changelog | Trivial |
| P3 | Normalization note for reserved-list command names with hyphens | Reader clarity |

---

## Overall

v4.2 is the cleanest revision cycle in the series — every v4.1
residual addressed, one production removed, zero new capabilities,
zero new bugs. Protocol is effectively feature-complete at this
point. The new issues are all minor polish (two headers/keys, one
default value, two typographic nits).

Eleven revision cycles. The protocol is ready to ship as a stable
1.0 standard under any naming the project prefers.
