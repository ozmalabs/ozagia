# Ozma Protocol v5.0 — Review (FINAL_final(1)_v2)

**Reviewer:** Claude (Opus 4.7)
**Date:** 2026-04-19
**Target:** `protocol.md` v5.0 (2026-04-19, 4082 lines)
**Priors:** `protocol-review-v4.3.md` and all predecessors.

v5.0 adds seven new capabilities spanning math, ML, schema/API work,
dependency hygiene, side-effect tracking, LLM-app editing, and
workflow-level state: **MATH**, **TENSOR-SHAPE**, **SCHEMA**, **DEP**,
**EFFECTS**, **PROMPT**, **AGENT-STATE**. This is the biggest
capability expansion since v2.0. Four grammar bugs, several smaller
issues, nothing structural.

---

## What landed in v5.0

### New data types

**MATH** — canonical Unicode math blocks with optional `[E1]` labels
and `{@math:E1}` cross-turn references. LaTeX normalization via
`X-Ozma-Math-Format`. Fills the gap the "math as a first-class item"
discussion identified.

**TENSOR-SHAPE** — named-dimension shape annotations with transform
arrows and `where` dimension algebra. Integrates with PyTorch
`torch.fx` / JAX `jax.eval_shape` for verification. The single most
impactful v5 primitive — ML code is where most shape bugs live and no
prior protocol captured them structurally.

**SCHEMA** — schema-edit operations for proto / openapi / graphql /
sql / avro / json-schema with compatibility tags (`compatible` /
`breaking` / `deprecation`). Shim diffs against prior cached schema
versions to catch mis-tagged breaking changes.

**DEP** — dependency operations (add / remove / upgrade / downgrade /
pin / audit) with CVE notes. Cross-references `backend/ozma/security/`
to auto-populate CVE references on upgrade.

**EFFECTS** — side-effect classification (reads / writes / network /
globals / pure). Static-analysis verified. Enables sandboxing and
pure-function reasoning.

**PROMPT** — prompt blocks embedded in LLM-app code. Shim runs
prompt-injection scanners and token-budget accounting against the
declared tokenizer.

**AGENT-STATE** — session-level workflow tracking (goal, progress,
subtasks, blockers). Persists across turns; enables agent resumption
from a persisted goal.

### Supporting changes

- Registry example extended with 7 new fingerprints.
- TURN grammar extended with new tail productions (SHAPE, EFFECTS,
  SCHEMA, DEP, MATH, PROMPT after BODY; AGENT_STATE near end).
- Capability profiles: `ml-code`, `api-code`, `agent`, `full-v5`.
- Shim responsibilities: 9 new rows covering MATH normalization,
  SHAPE verification, SCHEMA compat checking, DEP CVE lookup, EFFECTS
  static analysis, PROMPT validation, AGENT-STATE persistence.
- Telemetry: 22 new fields covering the full v5 surface.
- Training examples: 5 new (§15 TENSOR-SHAPE, §16 MATH+SCHEMA, §17
  PROMPT+EFFECTS, §18 DEP, §19 AGENT-STATE).

Major version bump — grammar fingerprint family changes force a new
LoRA training run regardless of whether individual caps are used.

---

## Blocking issues

### B1. Grammar production count in changelog is wrong

Changelog says "Grammar: 107 unique productions, CI-verified."

v4.4 was 89. v5.0 adds:

- MATH: 4 (MATH_LABEL, MATH_LINE, MATH, MATH_REF)
- TENSOR-SHAPE: 6 (SHAPE_NAME, SHAPE_DIM, SHAPE_VEC, SHAPE_BIND, SHAPE_SIDE, SHAPE)
- SCHEMA: 5 (SCHEMA_FMT, SCHEMA_OP, SCHEMA_COMPAT, SCHEMA_LINE, SCHEMA)
- DEP: 4 (DEP_OP, DEP_NOTE, DEP_LINE, DEP)
- EFFECTS: 6 (EFFECT_KEY, EFFECT_LIST, EFFECT_VAL, EFFECT_BIND, EFFECTS_LINE, EFFECTS)
- PROMPT: 1
- AGENT-STATE: 8 (TASK_STATUS, TASK_LINE, BLOCKER_LINE, AGENT_GOAL, AGENT_PROG, AGENT_SUBS, AGENT_BLOCK, AGENT_STATE)

Total added: 34. Actual count: **123**, not 107.

**Fix:** correct the changelog number; CI invariant should surface
this.

### B2. MATH and AGENT-STATE fingerprints differ by one character

```
"MATH":             "0c1d2e"
"AGENT-STATE":      "0c1d2f"
```

Visually indistinguishable. Fingerprint examples are illustrative
(hash values are arbitrary), but reviewers scanning for the cap-family
diff will easily conflate them. Regenerate one to a distinct prefix.

### B3. `DEP_LINE` requires a trailing space for no-PROSE form

```
DEP_LINE = "dep " , FILE , ": " , DEP_OP , " " , PROSE , [ DEP_NOTE ] , NEWLINE ;
```

`PROSE` can be empty, but the preceding `" "` is mandatory. So an
audit line with no prose but a note parses as `dep pyproject.toml:
audit [vulnerabilities=2]` requiring exactly one space between `audit`
and `[`. An audit line with no note at all requires `dep
pyproject.toml: audit \n` — trailing space before newline. Awkward
and error-prone.

**Fix:**

```
DEP_LINE = "dep " , FILE , ": " , DEP_OP ,
           [ " " , PROSE ] , [ " " , DEP_NOTE ] , NEWLINE ;
```

Make the whitespace-plus-content conditional.

### B4. `EFFECT_VAL` PROSE alternative overflows the line

```
EFFECT_VAL  = EFFECT_LIST | "none" | "true" | "false"
            | "http" | "smtp" | "dns" | "arbitrary" | PROSE ;
EFFECTS_LINE = "effects " , POSITION , ": " , EFFECT_BIND ,
               { " " , EFFECT_BIND } , NEWLINE ;
```

The PROSE alternative (for "specific hostname literal") is greedy —
`network=example.com writes=[...]` parses as
`EFFECT_VAL=example.com writes=[...]`, consuming the remaining
bindings. PEG ordered-choice doesn't save you here because PROSE
matches to end-of-line.

**Fix:** restrict PROSE to a hostname-like charset:

```
EFFECT_HOST = LCASE_WORD , { "." , LCASE_WORD } ;   (* no whitespace, dotted *)
EFFECT_VAL  = EFFECT_LIST
            | "none" | "true" | "false"
            | "http" | "smtp" | "dns" | "arbitrary"
            | EFFECT_HOST ;
```

### B5. `PROMPT` CODE_BLOCK tag is unconstrained

```
PROMPT = "prompt " , POSITION , ":" , NEWLINE , CODE_BLOCK ;
```

§PROMPT says the content should be a fenced code block with language
tag `text` or `prompt`. The grammar accepts any `CODE_BLOCK`, which
allows ```` ```python ```` to be parsed as a PROMPT. That
mis-identification bypasses the injection-resistance / token-budget
validators silently.

**Fix:**

```
PROMPT_TAG   = "text" | "prompt" ;
PROMPT_BLOCK = "```" , PROMPT_TAG , NEWLINE , { ? any line ? } , "```" ;
PROMPT       = "prompt " , POSITION , ":" , NEWLINE , PROMPT_BLOCK ;
```

---

## Medium issues

### M1. `SCHEMA_LINE` PROSE-before-COMPAT ambiguity

```
SCHEMA_LINE = "schema " , POSITION , " " , SCHEMA_FMT , ": " ,
              SCHEMA_OP , " " , PROSE , [ SCHEMA_COMPAT ] , NEWLINE ;
```

`PROSE` is greedy; `[compatible]` at the end can be absorbed into
PROSE ("add field email (string, optional, tag=7) [compatible]" →
PROSE might consume the full remainder). Same ambiguity class as
`HINT_LINE` / `EDGE` — add a parsing note in the same style:

```
(* PROSE DETAIL does not contain a trailing "[compatible]" / "[breaking]"
 * / "[deprecation]" substring. PEG: match SCHEMA_COMPAT greedily from
 * end of line, then PROSE is the middle. *)
```

### M2. `pure=true` shorthand conflict with explicit effects

```
effects §6: pure=true reads=[config.json]
```

The spec says `pure=true` is shorthand for `reads=[] writes=[]
network=none globals=[]`. If the model emits both, they contradict.
Grammar accepts it; shim behavior unspecified.

**Fix:** spec must state one of:

- `pure=true` and any other non-trivial binding together is a parse
  error → `effects_contradiction` logged.
- `pure=true` is a derived fact that MUST match the other bindings if
  both are present.
- `pure=true` wins and other bindings are ignored.

Any choice works; pick one and document it.

### M3. `MATH` block has no explicit terminator

```
MATH_LINE = "  " , PROSE , NEWLINE ;
MATH      = "math " , POSITION , [ " [" , MATH_LABEL , "]" ] , ":" ,
            NEWLINE , MATH_LINE , { MATH_LINE } ;
```

The `{ MATH_LINE }` continues until the first line that doesn't start
with `"  "`. Works for most cases but bites on:

- Two adjacent MATH blocks — the second `math §N:` line breaks the
  first (starts with `math`, not `  `). OK.
- A MATH block immediately followed by prose context at indent 2 —
  ambiguous which belongs to the math block.

**Fix:** either require an explicit terminator (`end math` or a blank
line) or document the "first non-indent-2 line terminates" rule
explicitly in §MATH.

### M4. `AGENT_STATE` persistence scope unclear

"Persists AGENT-STATE in the session cache keyed by session_id."

But §AGENT-STATE also says "an agent session killed mid-execution can
be resumed from the last persisted AGENT-STATE." Session cache is
session-scoped (dropped at session end per v1.6 semantics). If AGENT-
STATE is in the session cache, resumption across session boundaries
doesn't work.

**Fix:** AGENT-STATE should persist at `workspace` or `user` scope
(not `session`). Specify: "AGENT-STATE is persisted at workspace
scope by default, keyed by workspace_id + goal hash. Configurable to
user scope via registry key `agent_state_scope`."

### M5. `AGENT_STATE` requires full re-emission every turn

```
AGENT_STATE = "agent-state:" , NEWLINE ,
              AGENT_GOAL , AGENT_PROG , AGENT_SUBS , [ AGENT_BLOCK ] ;
```

Goal, progress, subtasks all required. No progress-only partial
update. Every turn that touches AGENT-STATE must re-emit the full
subtask list, which is wasteful on long workflows with many subtasks.

**Fix:** allow progress-only deltas:

```
AGENT_DELTA = "agent-state-delta:" , NEWLINE ,
              ( AGENT_PROG | AGENT_SUBS | AGENT_BLOCK ) ,
              { AGENT_PROG | AGENT_SUBS | AGENT_BLOCK } ;
```

Or make fields optional in `AGENT_STATE` after the first emission per
session.

### M6. AGENT_STATE appears once per turn in TURN grammar

`[ AGENT_STATE ]` — single occurrence. A turn that completes a plan
with 3 subtasks can't emit 3 agent-state updates. Probably fine (the
state at turn end is what matters) but worth noting — multi-update
workflows need other mechanisms.

### M7. New error kinds not in `§Error Type` enumeration

The new capabilities reference error kinds that aren't enumerated in
the `kind` table:

- `shape_violation` (TENSOR-SHAPE mismatch)
- `schema_compat_mismatch` (declared compat ≠ actual)
- `schema_breaking` (breaking change detected)
- `schema_format_illegal_op` (e.g., `add field` on SQL)
- `dep_cve_unresolved` (upgrade didn't resolve the referenced CVE)
- `effects_contradiction` (see M2)
- `prompt_injection_risk` (raw user interpolation detected)
- `prompt_token_budget_exceeded`
- `agent_goal_complete` — this is a success log, not an error; distinguish from error kinds.

**Fix:** extend the `kind` enumeration table in §Error Type with the
above (except the success-logs).

### M8. `progress` reuses `CONF_VAL` with different semantics

```
AGENT_PROG = "  progress: " , CONF_VAL , NEWLINE ;
```

`CONF_VAL` is "confidence in [0, 1]". `progress` is "fraction complete
in [0, 1]". Same type, different meaning. Reusing the production is
pragmatic but a reader could be confused.

**Fix:** add a comment ("progress reuses CONF_VAL's numeric form but
denotes completion fraction, not confidence") or introduce a
`PROGRESS_VAL = CONF_VAL` alias to make the intent explicit.

---

## Smaller issues

### S1. DEP upgrade format uses `→` inside PROSE

```
dep Cargo.toml: upgrade tokio 1.28 → 1.35 [CVE-2023-32681]
```

The `→` arrow is part of PROSE (not a structural grammar token).
Works fine but inconsistent with TENSOR-SHAPE (which uses `→`
structurally). Worth noting DEP's arrow is convention, not grammar.

### S2. TENSOR-SHAPE `where` clause is PROSE

Common documented-constraint pattern. Fine as-is. Worth a note that
the dimension algebra is advisory: the shim doesn't prove `D_head =
D_model / H`, only records the claim.

### S3. SCHEMA interaction with same-file EDIT

A turn can contain both `edit §6` (editing the schema file directly)
and `schema §6 proto: add field ... [compatible]`. Is the SCHEMA line
a *description* of the edit or a *command* the shim should execute?
Currently unclear.

**Fix:** specify — SCHEMA annotations describe intent; EDIT applies
the change; the shim cross-checks that the EDIT's delta matches the
declared SCHEMA operation.

### S4. PROMPT token budget tokenizer ambiguity

Token budget claims like `claim §6: token budget < 200 under
cl100k_base` are carried in CLAIM prose. The shim can parse the
tokenizer name from the claim. Consider a dedicated PROMPT field
instead:

```
prompt §6 tokenizer=cl100k_base:
```

### S5. Capability Profiles naming drift

Profile names mix hyphenated and non-hyphenated forms: `minimal`,
`standard`, `full`, `research`, `federated`, `ml-code`, `api-code`,
`agent`, `full-v5`. Inconsistent. Consider all-hyphenated (`ml-code`,
`api-code`, `full-v5`, `federated-v4`) or all-single-word.

### S6. `AGENT_STATE` doesn't reference positions in subtasks directly

```
- [in-progress] migrate login route (§6, §8)
```

Positions appear in prose, not structurally. Grammar doesn't parse
them. A structured form (`subtask=[in-progress] "migrate login route"
positions=§6,§8`) would let the orchestrator jump to relevant code.
Minor.

### S7. MATH `{@math:EN}` grammar disambiguation from HASH_REF

```
HASH_REF = "{@" , ( STABLE_ID | "prev" | "target" | "last_edit" ) , "}" ;
MATH_REF = "{@math:" , MATH_LABEL , "}" ;
```

`{@math:E1}` — `math:` prefix is unique (not STABLE_ID, not anaphora).
`STABLE_ID = HEX{8}` and `math` starts with `m` which is a valid hex
character but `math` is 4 chars, not 8. No actual ambiguity — the
parsers can dispatch on prefix. Good.

---

## Still open — from prior review lists (unchanged)

The v4.3 review's minor residuals remain unresolved in v5.0:

- `PLAN_STEP` is still PROSE-only (deliberate, per v4.3 comment).
- Session Cache "### Grammar (illustrative)" subsection heading.
- `strict_antispoof_active` / `silent_ack_ms` telemetry fields —
  added in v4.4, still present.
- Various forward-looking v6+ items (capability-level ACL,
  multi-turn formal-claim accumulation, etc.).

None regressed; none progressed.

---

## Priority for v5.1

| Priority | Fix | Why |
|----------|-----|-----|
| P0 | Correct grammar production count (123, not 107) | CI invariant |
| P0 | Regenerate MATH vs AGENT-STATE fingerprints (too similar) | Reviewer confusion |
| P0 | `DEP_LINE` optional PROSE+note whitespace | Currently requires awkward trailing space |
| P0 | `EFFECT_VAL` hostname alternative constrained | PROSE overflows line, corrupts subsequent bindings |
| P0 | `PROMPT_BLOCK` language tag enforcement | Untagged code blocks bypass prompt validators |
| P1 | `SCHEMA_LINE` compat-tag parsing note | PROSE ambiguity |
| P1 | `pure=true` contradiction rule | Currently unspecified |
| P1 | MATH block terminator rule (explicit or documented) | Ambiguous mid-context |
| P1 | AGENT-STATE scope (workspace, not session) | Resumption claim broken otherwise |
| P1 | AGENT-STATE delta form | Wasteful full re-emission |
| P1 | New error kinds in §Error Type enumeration | Completeness |
| P2 | `PROGRESS_VAL = CONF_VAL` alias for readability | Semantic clarity |
| P2 | SCHEMA + EDIT interaction rule | Ambiguity on same-file edits |
| P2 | Capability profile name consistency | Hyphenation drift |
| P3 | Structured position references in AGENT-STATE subtasks | Orchestrator ergonomics |
| P3 | PROMPT tokenizer field or CLAIM convention | Minor |
| P3 | DEP `→` vs TENSOR-SHAPE `→` note | Consistency |

---

## Overall

v5.0 is the biggest capability expansion since v2.0 and, unlike most
v-x.0 releases in this protocol's history, it ships with real
grammar-level bugs that would bite implementers immediately (B3, B4,
B5 especially). The design of the new capabilities is sound; the
grammar execution needs one more pass.

The set of additions is well-chosen:

- **TENSOR-SHAPE** is the highest-leverage ML-code primitive and fills
  a real capability gap.
- **MATH** with cross-turn refs is the minimum-viable math support;
  not over-built.
- **SCHEMA** and **DEP** are high-frequency edit classes that were
  previously generic EDITs; structural forms enable automation.
- **EFFECTS** is the purest training-signal primitive — static
  verification is cheap and drives calibration.
- **PROMPT** is the meta-case (LLMs editing LLM apps); small surface,
  real value.
- **AGENT-STATE** closes the turn-vs-session gap.

The grammar bugs are the blockers. Fix those plus the medium items
and v5.1 is genuinely shippable. Then we can call it *FINAL_final(1)
_v3.spec*.

Thirteen revision cycles. Protocol has crossed from "polish forever"
back into "real work to ship" — which is what happens when you add
seven capabilities at once.
