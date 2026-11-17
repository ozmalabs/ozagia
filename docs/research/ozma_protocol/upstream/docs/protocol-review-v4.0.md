# Ozma Protocol v4.0 — Review

**Reviewer:** Claude (Sonnet 4.6)
**Date:** 2026-04-19
**Target:** `protocol.md` v4.0 (2026-04-19, ~3300 lines)
**Priors:** `protocol-review-v3.0.md`

v4.0 lands all v3.0 review fixes cleanly. The new v4 capabilities (FEDERATED,
DP, GRAMMAR, cross-model identity) are well-motivated. Four blocking issues and
a set of medium/smaller issues need fixing before v4.1.

---

## What landed from v3.0 review

All blocking and P1–P3 items addressed:

- **CONF_ASPECT** disambiguated via `ASPECT_NAME` production; advisory `?`
  marker for review-gate exclusion.
- **POLYGLOT grammar** fully structured: `POLY_NAME`, `POLY_LANG`, `POLY_ID`,
  `POLY_BINDING`, `POLY_LINE` — no longer bare PROSE.
- **L2 registry fingerprint** corrected to 6 hex (`ab12cd`).
- **FORMAL-CLAIM counterexample format** normative (`[formal-claim §N refuted]`
  with `counterexample:` and `trace:` lines).
- **Canonical language tags** enumerated (`smt2`, `dafny`, `lean4`, `z3`);
  missing/unknown → `no_verifier/tag_missing`.
- **verifier_registry** added to L3 registry example.
- **STRUCTURED-CONF primary/advisory distinction** — `?` suffix marks advisory;
  gate uses min of primary aspects only.
- **POLYGLOT conflict resolution** — newest-wins, `[superseded]`, `merge_all`.
- **COLLAB advisory nature** documented; COLLAB_ENTRY accepts `CONF_VAL | CONF_MAP`.
- **PERSONA_ID** 32-char limit documented as shim-enforced.
- **POLYGLOT input format** specified (`[polyglot context]` prefix block).
- **POLY_NAME reserved-keyword policy** documented.
- **Session cross-model identity** rule added to Session Cache section.

---

## Blocking issues in v4.0

### B1. `DP_PARAM` uses `CONF_VAL` for epsilon and delta — wrong domain

```ebnf
DP_PARAM = "epsilon=" , CONF_VAL , " delta=" , CONF_VAL ;
```

`CONF_VAL = ( "0." , DIGIT , { DIGIT } ) | "1.0"` — accepts values only in
`(0, 1]`. But the section example shows:

```
dp: epsilon=0.1 delta=1e-5 composition=rdp
```

`1e-5` is not a valid `CONF_VAL`. Standard DP delta values are in the range
`1e-6`–`1e-5` (scientific notation). Epsilon values can also exceed 1.0 in
practice (ε=2.0, ε=10.0 are common). Using `CONF_VAL` is the wrong type.

**Fix:** Define a dedicated floating-point production for DP parameters:

```ebnf
DP_FLOAT = DIGIT , { DIGIT } , [ "." , DIGIT , { DIGIT } ]
         | "0." , DIGIT , { DIGIT }
         | DIGIT , { DIGIT } , "e-" , DIGIT , { DIGIT }
         | DIGIT , { DIGIT } , "e" , DIGIT , { DIGIT } ;
DP_PARAM = "epsilon=" , DP_FLOAT , " delta=" , DP_FLOAT ;
```

This accepts `0.1`, `1e-5`, `2.0`, `10`, `1e-6`.

### B2. `LCASE` and `LCASE_ALNUM` are defined under PERSONA but used much earlier

`ASPECT_NAME`, `POLY_NAME`, `POLY_LANG`, and `EMBED_ID` all reference `LCASE`
and `LCASE_ALNUM`. These are defined hundreds of lines later, under the PERSONA
grammar section. Forward references are syntactically permissible in EBNF but
cause real problems for parser generators that process productions top-to-bottom
(Lark, ANTLR, PEG parsers). They also violate the spec's own readability goal:
a parser reading the shared primitives section has no definitions for `LCASE`.

**Fix:** Move `LCASE` and `LCASE_ALNUM` into the `(* ── Shared primitives ──)`
block at the top of the grammar.

### B3. `ASPECT_NAME`, `POLY_NAME`, `POLY_LANG` allow `-` via `LCASE_ALNUM`

```ebnf
LCASE_ALNUM = LCASE | DIGIT | "_" | "-" ;
ASPECT_NAME = LCASE , { LCASE_ALNUM | "_" } ;  (* claims [a-z][a-z0-9_]* *)
POLY_NAME   = LCASE , { LCASE_ALNUM | "_" } ;  (* claims [a-z][a-z0-9_]* *)
POLY_LANG   = LCASE , { LCASE_ALNUM | "_" } ;
```

`LCASE_ALNUM` includes `"-"` (needed for `PERSONA_ID` to allow `code-reviewer`
style names). `ASPECT_NAME` claims to restrict to `[a-z][a-z0-9_]*` — no
hyphen. `POLY_NAME` and `POLY_LANG` should also forbid hyphen (language tags
like `python`, `go`, `ts` don't use hyphens; canonical names like
`validate_token` don't either). But the grammar currently allows `return-type`
and `formal-claim` as aspect names and poly-names.

The prose is correct; the grammar is wrong.

**Fix:** Introduce a separate primitive that excludes `-`:

```ebnf
LCASE_WORD  = LCASE | DIGIT | "_" ;   (* [a-z0-9_] — no hyphen *)
ASPECT_NAME = LCASE , { LCASE_WORD } ;
POLY_NAME   = LCASE , { LCASE_WORD } ;
POLY_LANG   = LCASE , { LCASE_WORD } ;
```

`LCASE_ALNUM` retains `-` for `PERSONA_ID` and `EMBED_ID` (embed alias names
like `embed:auth-svc` are reasonable).

### B4. `FORMAL_CLAIM` appears before `BODY` in `TURN` — contradicts training examples

```ebnf
TURN = [ ACK ] ,
       [ REASONING ] ,
       [ RAT_BLOCK ] ,
       [ CLAIM_BLOCK ] ,
       { FORMAL_CLAIM } ,   (* ← BEFORE plan and body *)
       [ PLAN ] ,
       BODY ,
       ...
```

But training example 13 shows:

```
rationale §6: ...
edit §6 confidence={...}
```python
...
```
formal-claim §6:
```smt2
...
```
```

The edit comes first, then the formal claim. This is the correct semantic order:
you edit a position and then assert a formal invariant about the result. Placing
`FORMAL_CLAIM` before `BODY` requires the model to assert formal properties about
an edit it has not yet emitted — backwards.

**Fix:** Move `{ FORMAL_CLAIM }` to after `BODY`, before `{ MEMORY_CMD }`:

```ebnf
BODY ,
{ FORMAL_CLAIM } ,    (* after edit — claim about the edited position *)
{ MEMORY_CMD } ,
```

---

## Medium issues

### M1. Training example 11 (LEARNED-BPE) uses uppercase `B1`/`B2` aliases

```
<legend>
B1=UserAuthenticationService, B2=validate_and_refresh_token
</legend>
```

The LEARNED-BPE section says: "The aliases are deterministic: `b1`, `b2`, …
(lowercase, consistent with the alias namespace convention)." And: "Namespace
reservation: The `b`-prefix (`b1`–`b99`) is reserved for BPE aliases."
Lowercase `b`. The training example uses uppercase `B1`, `B2` — a direct
contradiction of the spec. The LoRA trained on this example will learn to
produce uppercase BPE aliases that the namespace reservation (case-sensitive)
does not cover.

**Fix:** Change training example 11 to `b1`, `b2` throughout.

### M2. FORMAL-CLAIM shim step 1 only mentions `tag_missing`, not `tag_unknown`

The "Canonical language tags" paragraph correctly distinguishes:
- Missing tag → `tag_missing`
- Non-standard/unrecognized tag (`smt`, `smtlib`) → `tag_unknown`

But shim step 1 says: "If not one of `smt2 | dafny | lean4 | z3`, records
`no_verifier/tag_missing` and stops." A tag of `smt` is present but unrecognized
— the correct sub-kind is `tag_unknown`, not `tag_missing`. The shim behavior
collapses both to `tag_missing`.

**Fix:** Differentiate in step 1:
- Tag absent: `no_verifier/tag_missing`
- Tag present but not in canonical set: `no_verifier/tag_unknown`

### M3. Telemetry schema missing v4 fields

The telemetry schema example does not include fields for the four v4 capabilities:
- `federated_positions`: count of FEDERATED annotations emitted
- `dp_epsilon`: epsilon from DP marker (null if absent)
- `dp_delta`: delta from DP marker (null if absent)
- `model_switch_cap_delta`: list of capabilities that changed on model switch
- `formal_verify_advisory_aspects`: count of advisory aspects excluded from gate

Without these fields, the training pipeline cannot monitor v4 capability usage.

### M4. Profile table — `federated` profile is ambiguous about cumulative caps

```
| `federated` | All v4.0 caps | 70B+ | FEDERATED, DP, GRAMMAR — multi-node training pipelines |
```

"All v4.0 caps" is ambiguous: does it mean only the three new v4 caps, or
all caps including v1–v3? A federated training pipeline needs the full
protocol stack (FORMAL-CLAIM for verification labels, COLLAB for local weights,
etc.), not just the three new v4 caps. The description should be explicit.

**Fix:** Change to "Full + research + v4.0 caps" or "All caps through v4.0".

### M5. Extension Points header table missing v4 headers

No headers are defined for v4 capabilities:
- `X-Ozma-DP-Epsilon-Cap`: per-deployment epsilon budget cap
- `X-Ozma-DP-Delta-Cap`: per-deployment delta budget cap
- `X-Ozma-Federated-Round`: override the current federated round number

Without these headers, operators have no documented way to configure DP budget
caps or federated round state via the API.

### M6. `POLY_ID` allows empty string

```ebnf
POLY_ID = { ? any char except "," and newline ? } ;
```

`{ ... }` in EBNF means zero or more repetitions — an empty `POLY_ID` is
syntactically valid. A binding like `go=` (empty identifier) would parse.
The shim's CodeIndex validation would catch this, but the grammar should
reject it.

**Fix:** Require at least one character: `POLY_ID = ? any char except "," and newline ? , { ? any char except "," and newline ? }`.

---

## Smaller issues

### S1. `recall:` typo in shim responsibilities table

```
| Respond to `recall:` queries from memory store | — | ✓ | ✓ | ✓ |
```

Should be `recall` (no colon). `recall` is the command name; `recall:` is
not the syntax.

### S2. `ASPECT_NAME` grammar has redundant `"_"` alternative

```ebnf
ASPECT_NAME = LCASE , { LCASE_ALNUM | "_" } ;
```

`LCASE_ALNUM` already includes `"_"`, so `{ LCASE_ALNUM | "_" }` is equivalent
to `{ LCASE_ALNUM }`. The extra `| "_"` is dead. Cleaned up by the B3 fix
(switching to `LCASE_WORD`), but worth noting.

### S3. Extension Points "v1.x" versioning claim is stale

"A v1.x shim accepts acks from any v1.y model (y >= x)" — the protocol is
now at v4.0. The versioning rule should be restated for the current major
version, or generalized: "A vN.x shim accepts acks from any vN.y model
(y ≥ x) for the capabilities it knows about."

### S4. GRAMMAR section — informal "current grammar version is 4.0" statement

"The current grammar version is `4.0`." This mixes a protocol version number
with a grammar semantic version. The GRAMMAR fingerprint (`789abc`) is the
actual machine-readable identifier; the string "4.0" is ambiguous with the
protocol version. Clarify: "The current grammar version corresponds to
protocol v4.0; the authoritative identifier is the GRAMMAR fingerprint in
the probe/ack."

### S5. Training example ordering — v1.2 ack in examples 1–3

Training examples 1, 2, 3 (supervised, correction, interaction) use
`[OZMA v1.2 ack:deadbeef ...]`. These are the oldest examples and were never
bumped to v3.0/v4.0. All examples in §Training Data Format should be
consistent with the current version.

---

## Priority for v4.1

| Priority | Fix |
|----------|-----|
| P0 | `DP_PARAM`: replace `CONF_VAL` with `DP_FLOAT` supporting scientific notation |
| P0 | Move `LCASE`/`LCASE_ALNUM` to shared primitives section |
| P0 | Add `LCASE_WORD` (no `-`); use for `ASPECT_NAME`, `POLY_NAME`, `POLY_LANG` |
| P0 | Move `{ FORMAL_CLAIM }` to after `BODY` in TURN production |
| P1 | Fix training example 11: `B1`/`B2` → `b1`/`b2` |
| P1 | FORMAL-CLAIM step 1: distinguish `tag_missing` vs `tag_unknown` |
| P1 | Add v4 telemetry fields to schema example |
| P2 | Profile table: clarify `federated` = cumulative "all caps through v4.0" |
| P2 | Header table: add `X-Ozma-DP-Epsilon-Cap`, `X-Ozma-DP-Delta-Cap`, `X-Ozma-Federated-Round` |
| P2 | `POLY_ID`: require at least one character |
| P3 | Fix `recall:` typo in shim table |
| P3 | Fix versioning claim in Extension Points |
| P3 | Clarify GRAMMAR "version 4.0" informal statement |
| P3 | Bump stale `v1.2 ack` in training examples 1–3 to v4.0 |

---

## Overall

v4.0 is solid: eight review cycles, every prior priority list closed out.
The v3.0 fixes all landed correctly. The four blocking issues are confined
to the grammar (DP_PARAM type mismatch, LCASE ordering, `-` in identifiers,
FORMAL_CLAIM placement). All are small EBNF edits — a few hours of work.
The most operationally significant fix is DP_PARAM: the example `delta=1e-5`
is unrepresentable in the current grammar, making the DP capability
non-functional from day one.
