# Ozma Shim Requirements

**Version:** 5.16  
**Status:** Draft  
**Companion spec:** [`protocol.md`](./protocol.md) — the Model Language specification. This document describes what the shim must do to honour the protocol on a given deployment; `protocol.md` describes what the model emits and receives at the token boundary.

**Layer boundary.** The protocol/shim split is absolute:
- **protocol.md** = invariant envelope grammar + semantics. Stable across all deployments.
- **shim.md** = per-deployment runtime behaviour. Varies with platform, client, backend capabilities, tool availability.

The canonical example: "emit a GRAPH datatype" is protocol (model language); "render a graph as mermaid on this client" is shim (deployment decision). Any paragraph that starts "the shim does X when the model emits Y" should have Y's envelope in protocol.md and X's behaviour in shim.md.

**Map literacy.** The protocol is a system for exchanging semantic maps (see protocol.md§Protocol as Maps). The shim's job is to generate, transmit, cache, and apply maps. Wire-compression maps (corpus-derived substitution tables, identifier aliases) are entirely shim concerns — the protocol grammar never sees them.

---

## Runtime Teaching (universal mechanism)

Three places in the protocol use the same teaching mechanism:

| Direction | Capability | Teaches | Section |
|---|---|---|---|
| shim → model | UPGRADE (v5.3) | Protocol capabilities the model did not learn at training time | §Self-Upgrade below |
| model → client | CAP-PROBE (v5.7) | Client renderer capabilities the model did not know about | §Handshake extensions |
| agent → peer-agent | COORDINATION (v5.9) | Native-Ozma wire profile when peer is capable but defaults to A2A | §Peer-to-Peer Coordination |

All three share this mechanism; this section specifies the
common shape so downstream sections reference rather than
re-specify.

### Shape (universal)

1. **Initiator** injects a teaching prompt into context — a
   versioned, canonical block describing the feature being taught,
   with sufficient examples for a capable model to parrot the
   format on output.
2. **Target** either (a) acks understanding in structured form
   (`cap_probe_ack: understood`, `ozma_compression_ack: understood`,
   etc.) or (b) simply produces the taught format on its next turn.
3. **Two-way verification** — the first 2–3 exchanges after teaching
   carry *both* forms (taught + standard/untaught) so the initiator
   can verify round-trip decode succeeds.
4. **Single-flip revert** — any decode failure, malformed reply,
   or protocol error on the target's side triggers abort: teaching
   is reverted for this peer/session; standard form used thereafter;
   no retry within the session.
5. **Caching** — once a peer identity has been successfully taught,
   subsequent sessions remember (via session cache or deployment
   registry) and skip re-teaching unless the teaching version
   changes.

### Tiers (universal)

Every teaching instance picks one of three tiers based on
target-model-tier estimate and expected interaction volume:

| Tier | Mechanism | Token cost | When |
|---|---|---|---|
| `untaught` | Default / standard form only | 0 | Target is L0 or L1 `prompted`, or one-shot exchange |
| `inline-hinted` | ~50–100-token legend embedded on first exchange | ~80 toks once | Capable target (trained L1 or L2) decodes on first read; <5 exchanges expected |
| `pre-taught` | Full teaching prompt at handshake (~300–500 toks); target acks | ~500 toks once | Extended conversation expected (>5 exchanges); target is trained L1 or L2 |

Breakeven: inline-hinted at ~2 exchanges, pre-taught at ~2
exchanges. Net positive for any conversation beyond one round.

### Telemetry fields (universal)

Every teaching instance contributes to the trace tree with
`kind=teaching` entries:

- `teaching_direction` — `shim_to_model | model_to_client |
  agent_to_peer`
- `teaching_target_id` — model id, client id, or peer agent id
- `teaching_tier` — `untaught | inline-hinted | pre-taught`
- `teaching_feature` — e.g., `STYLE`, `rich-html`, `ozma-native-wire`
- `teaching_outcome` — `succeeded | reverted | skipped | cached`
- `teaching_revert_reason` — populated when `outcome=reverted`
- `teaching_tokens_overhead` — realized one-time cost
- `teaching_savings_tokens_realized` — cumulative savings over the
  session/conversation

### Why it generalizes

The ceiling is target model capability, not protocol. Any
structural feature a capable model can parrot from a teaching
prompt is teachable. The three places above are the current
applications; future capabilities can use the same mechanism
without re-inventing it.

---

## Verification Continuum (v5.9.1)

The protocol offers many ways to answer *"is this correct?"* Each
trades off between speed, cost, rigor, and who does the checking.
Authors often don't know which to pick; this reference orients
them along a soft-to-hard continuum:

```
  CONFIDENCE scale   →   CLAIM   →   FORMAL-CLAIM   →   verify-dispatch   →   peer review   →   human-gate
   (self-reported)       (asserted)    (proof-checked)     (N-model)           (peer-judged)     (human-judged)

  softest  ─────────────────────────────────────────────────────────────────────────────────────  hardest
  cheapest                                                                                           slowest
  self                                                                                             external
```

| Surface | What it does | Cost per use | When to pick |
|---|---|---|---|
| `CONFIDENCE` scale on an edit or claim | Self-reported 0.0–1.0; triggers downstream review thresholds | ~0 tokens (single float) | Always-on; free; drives other surfaces |
| `CLAIM §N: <invariant>` (v1.2) | Named invariant asserted about the edit; stored for follow-up verification; non-blocking | ~1 line per claim | Assertions about behavior that *should* hold |
| `FORMAL-CLAIM smt2/dafny/lean4` (v3.0) | Machine-verifiable via bundled verifier; shim returns counterexample on refute | Verifier time (100ms–10s) | Invariants where a proof-style check is cheap (SMT-friendly) |
| `[dispatch strategy=verify n=N]` (v5.8) | N models check a hypothesis; correlated-error reduction | N × per-model cost (seconds) | High-stakes claims; easy to frame as hypothesis |
| Peer `review-purpose request` (v5.9) | Explicit human-reviewed-by-another-agent flow; async | Peer agent turn cost (seconds–minutes) | Subjective judgment, design decisions, architectural choices |
| `[gate kind=human]` (v5.9) | Human approver gates the action; fatigue-mitigated via rising_threshold etc. | Human time (minutes–days) | Destructive / irreversible / high-blast-radius |

**How to pick (heuristic):**

- **Reversibility × stakes.** Reversible + low-stakes → CONFIDENCE
  alone may be enough. Irreversible + high-stakes → every surface
  in sequence.
- **Deterministic check available?** If yes (SMT, type-check,
  runtime assertion), prefer FORMAL-CLAIM — it's by far the best
  correctness-per-dollar.
- **Subjective judgment needed?** FORMAL-CLAIM can't evaluate
  design quality. Escalate to peer review or human-gate.
- **Budget:** cheaper surfaces are always preferred when
  sufficient. The CONFIDENCE threshold mechanic (v1.2) is
  designed to short-circuit — high confidence skips review,
  low confidence escalates.

**Composition.** The surfaces stack. A typical high-stakes flow:

1. Model emits edit with `CONFIDENCE: 0.85`.
2. Accompanies with `CLAIM §6: never raises on malformed JSON`.
3. Shim auto-invokes `FORMAL-CLAIM` verifier where registered.
4. If verifier refutes or claim is complex, triggers
   `[dispatch strategy=verify n=5]` across diverse models.
5. If consensus < `confidence_floor`, opens a peer
   `review-purpose request` to a reviewer agent.
6. If reviewer returns `changes-requested`, routes to
   `[gate kind=human]` for final adjudication.

Each layer catches what the layer below missed; cost increases
monotonically. In practice most edits stop at step 1 or 2.

---

## Self-Upgrade (runtime capability teaching)

*Instance: shim → model direction (see §Runtime Teaching for the
universal mechanism).*

The protocol evolves faster than LoRA training cycles. A model
fine-tuned on v4 should not need retraining to use a v5 capability,
and a model fine-tuned on the *core* (EDIT, INTERACT, RATIONALE,
CLAIM, MEMORY) should be able to pick up any later capability
via a runtime teaching prompt.

Self-upgrade is that mechanism: a compact, canonical teaching prompt
per capability, injected into context only when the model has acked
the capability as `accept` (§Handshake) rather than `trained`. The
model sees the teaching prompt plus the normal request; output is
parsed with the same grammar as for trained caps, but format validity
is teaching-quality (`accept`) rather than trained-quality.

### Goals

- **Decouple protocol evolution from retraining cadence.** New caps
  ship; models adopt them immediately at degraded quality; the next
  training cycle promotes `accept` → `trained`.
- **Graceful degradation across time.** Mirrors the L0 / L1 (retrofit
  depth) / L2 downward degradation with an equivalent upward
  degradation: any core-trained model is a base from which all future
  caps are reachable.
- **Cheap to ship.** Adding a capability is registry + teaching
  prompt + grammar. Training on it is a later optimization.

### Mechanism

1. Model ack lists cap in `accept:` section.
2. On each request that would use the cap, the shim injects the
   canonical teaching prompt into the system message after the probe
   line (stable cache position).
3. Model follows the prompt to emit output in the declared format.
4. Shim parses with the normal grammar, applies validation, tracks
   format-validity rate.
5. If format-validity for a prompt-taught cap drops below threshold
   (default 70%) over a rolling window (default 20 emissions), the
   shim circuit-breaks the cap for the remainder of the session with
   `teaching_fallback_exhausted` logged. The client sees
   `X-Ozma-Disabled: <CAP>` on the next response.

### Teaching prompt format

Teaching prompts are declarative, compact, and stable. One per
capability, versioned alongside the capability's grammar fingerprint
— changing the grammar bumps the fingerprint and produces a new
teaching prompt.

```yaml
# ozmalib/teaching_prompts/rationale.yaml
capability: RATIONALE
grammar_fingerprint: e1a7f4
version: 1
tokens_budget: 80                # approximate length after rendering
prompt: |
  When you modify a code position, emit a rationale line immediately
  after the edit using this format:
    rationale §N: <one-line explanation>
  One rationale per changed position. Example:
    rationale §6: added empty-file guard
```

Fields:

- `capability` — capability name, matches the registry.
- `grammar_fingerprint` — MUST match the capability's grammar
  fingerprint. Teaching prompt and grammar move together.
- `version` — monotonic integer, bumps on teaching-prompt refinement
  without grammar change (e.g., clearer wording).
- `tokens_budget` — advisory budget under cl100k_base; teaching
  prompts over 200 tokens are a smell; most should be 40-120 tokens.
- `prompt` — the rendered text. SHOULD be 2-5 sentences + 1-2
  examples. MUST describe the output format unambiguously enough that
  a model with no training on the cap can produce parseable output.

**Budget discipline.** The combined size of all active teaching
prompts in a session is bounded. Default: 600 tokens total across
all prompt-taught caps per request. If the budget is exceeded, the
shim prioritizes caps the model actually exercised in recent turns
and drops teaching prompts for unused caps.

### Ordering in the system message

Teaching prompts are injected after the probe line, before any
per-turn user-prefix content. This keeps them in the cache-stable
prefix:

```
[System message]
  Ozma probe line                          ← retrofit-trained L1 or L2 only
  Protocol instruction                     ← full-retrofit L1 or L2 only
  Teaching prompts for accepted caps       ← v5.2+ — stable, cache-friendly
  
[User-role prefix]
  <legend>…</legend>                       ← per-turn state
  ...
```

Stability of this ordering is what makes self-upgrade cache-efficient.
As long as the accepted-cap set doesn't change within a session,
teaching prompts stay at the same prefix position and benefit from
upstream prompt caching.

### Quality expectations

Teaching prompts are standard instruction-following content. Any
capable model reads them. What `UPGRADE` awareness changes is the
*quality* of prompt-taught output, not whether teaching prompts are
delivered. The degradation is continuous across tiers:

| Tier | Cap status + UPGRADE status | Format validity | Quality |
|------|-----------------------------|-----------------|---------|
| native-trained | `trained:` matching fingerprint | ~100% | Native — trained on the cap's format |
| upgrade-trained | `accept:` + UPGRADE `trained` | ~85-95% | Prompt-taught at full-retrofit fluency — model knows the self-upgrade metaphors |
| upgrade-accept | `accept:` + UPGRADE `accept` | ~75-90% | Prompt-taught, protocol-aware; reads teaching prompts as structured instruction |
| instruction-tier | `accept:` + no UPGRADE ack | ~60-85% | Teaching prompt treated as plain natural-language instruction; any instruction-following model |
| prose-only | No ack at all | N/A | Prose only, no structured output attempted |

Key point: the instruction-tier tier is where most non-Ozma models land
when a deployment wants to use Ozma capabilities with them. A stock
GPT-5 or Claude Sonnet reading a teaching prompt *is* instruction-
following — and instruction-following is what frontier models do
well. Format validity in the 60-85% range is often acceptable
especially when combined with the correction loop (§Scope Check
Protocol).

Prompt-taught caps should never be relied on for safety-critical
invariants (e.g., FORMAL-CLAIM verification correctness) without an
external verifier, regardless of tier. Validation of prompt-taught
output catches format drift; it does not improve semantic correctness
over training.

### What cannot be prompt-taught

Some capabilities depend on training in a way that a runtime prompt
cannot bridge:

- **GRAMMAR** shared-primitive fingerprint. This is structural —
  the shared primitives (CONF_VAL, POSITION, etc.) are referenced
  by many caps. A teaching prompt for GRAMMAR is incoherent; if the
  shared primitives don't match, the whole stack falls back to
  prose.

Registry declares `teachable: false` for these caps. The shim rejects
`accept:` entries for non-teachable caps with `cap_not_teachable`
logged, treats them as prose fallback.

### Telemetry

Prompt-taught caps are the feedback loop for the next LoRA version.
Every prompt-taught cap emission records:

- `cap_emitted_as_prompt_taught` — capability name.
- `cap_format_valid` — boolean: did the output parse cleanly?
- `cap_teaching_prompt_version` — which teaching-prompt version was
  active.
- `cap_teaching_prompt_tokens` — actual token cost of the teaching
  prompt for this session.

Training pipeline filter: a cap with high `accept` usage and high
format-validity rate is a candidate for promotion to `trained` in
the next LoRA revision. A cap with high usage and low validity is a
signal that the teaching prompt needs refinement or the cap is
fundamentally prompt-hostile.

### Self-upgrade fingerprint (`UPGRADE` capability)

The self-upgrade mechanism itself is versioned. A new capability
`UPGRADE` in the registry carries the fingerprint of the self-upgrade
protocol itself — the format of teaching prompts, the ack `accept:`
section, circuit-breaker rules. Models that ack `UPGRADE:<fp>` signal
that they understand the self-upgrade protocol natively, and will
treat teaching prompts as protocol-aware structured instruction
rather than plain natural-language guidance.

**UPGRADE is not a gate; it is a quality signal.** Teaching prompts
are injected regardless of whether the model acked UPGRADE. Any
instruction-following model reads them. The difference:

- With `UPGRADE: trained` — the model has learned the self-upgrade
  metaphors (teaching-prompt format, `accept:` semantics,
  circuit-breaker implications). Prompt-taught output lands at
  upgrade-trained quality (~85-95% format validity).
- With `UPGRADE: accept` — the model reads the UPGRADE teaching
  prompt itself to bootstrap: it now knows how to read other
  teaching prompts. Quality sits at upgrade-accept (~75-90%).
- Without any UPGRADE ack — the model treats teaching prompts as
  plain natural-language instruction. It does not have the meta-
  understanding of the self-upgrade layer; it simply follows the
  capability's teaching prompt the way any model follows any
  structured instruction. Quality lands at instruction-tier (~60-85%).

The `UPGRADE` capability therefore lifts an entire deployment's
prompt-taught-cap quality without requiring per-cap training. Models
with `UPGRADE: trained` in their ack handle every future capability
at strong quality; models without it still work, just at
instruction-tier quality.

**The UPGRADE teaching prompt is self-bootstrapping.** It describes
the self-upgrade protocol *in* a teaching prompt, so a model that
acks `UPGRADE: accept` reads that teaching prompt first and comes
up to upgrade-accept quality on subsequent teaching prompts.

This is the lever that makes "any core-trained model, any capability"
work at reasonable quality: `UPGRADE` is the one capability worth
retraining for, because it raises the quality floor on every other
prompt-taught capability.

### Registry additions

```python
MODEL_CAPS = {
    "qwen3.6-ozma-core-v1": {      # minimal core + upgrade
        "level": 1,
        "caps": {
            "OUTLINE":   "a4f9c1",
            "EDIT":      "b2c1d3",
            "INTERACT":  "d8f3e2",
            "RATIONALE": "e1a7f4",
            "CLAIM":     "c3d4b5",
            "MEMORY":    "cd34ef",
            "UPGRADE":   "0123ab",  # v5.3+ — self-upgrade protocol (quality signal)
        },
        "constrained_decode": true,
    },
}
```

And per-capability teaching-prompt declarations:

```python
CAPABILITY_META = {
    "MATH":         { "teachable": true,  "teaching_prompt": "math.yaml"  },
    "TENSOR-SHAPE": { "teachable": true,  "teaching_prompt": "tensor-shape.yaml" },
    "SCHEMA":       { "teachable": true,  "teaching_prompt": "schema.yaml" },
    "GRAMMAR":      { "teachable": false },  # structural — shared-primitive fingerprint
    # ... rest of caps
}
```

### Version-inferred implicit accept (upgrading existing models)

The ack header declares the protocol version the model was trained
against: `[OZMA v5.1 ack:...]`. Because each protocol version has a
canonical capability set (documented in the §Changelog at the top of
this spec), the shim can infer which caps are new to a model without
any registry-side configuration.

**The rule:** a cap that exists in the current protocol version but
was introduced *after* the model's declared training version is
implicitly accepted — the shim injects a teaching prompt for it
exactly as if the model had listed it in `accept:`.

**Resolution order** per cap, per request:

1. Cap is in model's `trained:` ack with matching fingerprint →
   native-trained quality, no teaching prompt.
2. Cap is in model's `accept:` ack → teaching prompt injected,
   upgrade-trained or upgrade-accept quality depending on UPGRADE
   status.
3. Cap exists in current protocol version but not in model's
   declared training version → **version-inferred implicit
   accept**. Teaching prompt injected, instruction-tier or
   upgrade-accept quality.
4. Cap is declared `teachable: false` (GRAMMAR, and any other
   structural caps) and not in `trained:` → prose fallback, no
   teaching prompt.
5. Otherwise → prose fallback.

**Cap-set catalog per version.** The shim ships with a catalog
mapping each protocol version to the cap set introduced in or before
that version:

```python
VERSION_CAP_SETS = {
    "v1.0": {"OUTLINE", "EDIT", "INTERACT", "HINT", "GRAPH", "TREE"},
    "v1.2": {... + "RATIONALE", "CLAIM", "HINT", "REASONING", "PLAN",
                    "TEST-SPEC", "HANDOFF", "WIRE"},
    "v1.5": {... + "MEMORY", "PERSONA"},
    "v2.0": {... + "ATTN", "DOCSTUB", "EMBED"},
    "v3.0": {... + "STRUCTURED-CONF", "FORMAL-CLAIM", "POLYGLOT", "COLLAB"},
    "v4.0": {... + "FEDERATED", "DP", "GRAMMAR"},
    "v5.0": {... + "MATH", "TENSOR-SHAPE", "SCHEMA", "DEP",
                    "EFFECTS", "PROMPT", "AGENT-STATE"},
    "v5.3": {... + "UPGRADE"},
    "v5.4": {... + "STYLE", "THEME", "PROSODY", "VOICE"},
    "v5.5": {... + "AVATAR", "MOTION"},
    "v5.7": {... + "VISION", "LISTEN", "SENSOR",
                    "VISION-ANNOTATE", "VISION-EDIT", "CAP-PROBE"},
    "v5.8": {... + "ROUTING-CONTEXT", "WORK-UNIT", "DISPATCH"},
    "v5.9":   {... + "COORDINATION"},
    "v5.9.2": {... + "SESSION-REF"},
    "v5.10":  {... + "SPECIALIST-DISPATCH", "VISION-DISPATCH", "LISTEN-DISPATCH", "SENSOR-DISPATCH"},
}
```

The `VERSION_CAP_SETS` diff-based auto-teaching path (§Runtime Teaching) is **gated off for sessions where the model is registered with `constrained_decode: true`**. Constrained-decode sessions never receive teaching prompts regardless of the version delta between the shim and the model's declared version — their cap set is fixed at training time by the decoder grammar and cannot be extended at runtime. A v5.10 shim injects teaching prompts for `SPECIALIST-DISPATCH` into any session where the model acks `ROUTING-CONTEXT` as trained but lacks `SPECIALIST-DISPATCH`. The three named-form caps (`VISION-DISPATCH`, `LISTEN-DISPATCH`, `SENSOR-DISPATCH`) are taught only when the model also acks the corresponding ambient cap (`VISION`, `LISTEN`, `SENSOR`). A model that acks none of those ambient caps still receives `SPECIALIST-DISPATCH` teaching (enabling `kind=custom`). A v5.1 model acks the v5.0 cap set as trained. Current shim at v5.3
injects teaching prompts for every cap in `v5.3 \ v5.0` — namely
`UPGRADE` (and any other caps added in v5.1 - v5.3 minor bumps).

**Zero registry change required.** A v5.1-trained model acking
`[OZMA v5.1 ack: ...]` picks up every v5.2+ capability immediately
when the shim is upgraded to v5.3. No per-model config. No new
LoRA. The protocol knows its own version history; the shim reads
the declared version and derives the upgrade set.

**Upgrade path for existing models:** the entire path is a shim
upgrade on the deployment side.

1. Deploy the v5.3 shim. Ship the teaching-prompt library.
2. Existing models automatically start receiving teaching prompts
   for capabilities introduced after their trained version.
3. Output arrives at instruction-tier quality (~60-85% format
   validity) per cap; correction loop catches most format failures.
4. Next retraining cycle can promote high-usage prompt-taught caps
   to `trained:` based on telemetry.

The next revision moves targeted caps from instruction-tier →
native-trained; the only meaningful training-time addition is `UPGRADE`,
which raises baseline prompt-taught quality for every other cap
from instruction-tier to upgrade-accept (~+15pp format validity).

### Registry `implicit_accept` — explicit override (optional)

Version inference is the default path. `implicit_accept` remains as
an optional registry key for explicit overrides:

```python
"qwen3.6-ozma-coder-v5.1": {
    "caps": { ... },
    # Version inference already covers v5.2+ caps. These are
    # optional overrides:
    "implicit_accept": {
        "exclude": ["DP", "FEDERATED"],   # don't auto-teach these
        "include": ["UPGRADE"],            # force-teach even if in current version
    },
}
```

Use cases:

- **Narrow a model** that was trained on v5.1 but for a specific
  deployment shouldn't use DP / FEDERATED (out of scope):
  `exclude` them.
- **Expand a model** with an experimental teaching prompt the
  version catalog doesn't yet know about: `include` it.

**Client header** for per-request override, unchanged:

```
X-Ozma-Implicit-Accept: MATH,TENSOR-SHAPE,SCHEMA
X-Ozma-Implicit-Accept: *
```

`*` forces every teachable cap not already in `trained:`, regardless
of version inference. Useful for experiments.

### The economic argument

The self-upgrade mechanism is the difference between "ship a new LoRA
every protocol version" and "train a core LoRA once, add capabilities
as teaching prompts." For a project expecting 2-4 protocol revisions
per year, this cuts training costs by ~70-80% across the lifecycle:

- v1 core LoRA: full training (~$5-15K compute).
- v1.1, v1.2, … v5.x: teaching prompts only ($0 training cost per
  capability addition).
- Periodic "consolidation" LoRA retrains bring `accept` caps into
  `trained` set based on usage telemetry, roughly annually.

The quality tradeoff: prompt-taught caps are 70-90% format-valid vs
~100% for trained. For caps with lower criticality or newer caps
still stabilizing in grammar, this is acceptable. For mature caps
heavily used, promote to `trained` on the next retrain.

---

## Session Cache

The dominant cost in long agent sessions is not any single turn — it is
repetition. The same outline, call graph, and log are re-sent every turn.
The session cache makes context stateful and sparse.

### Content-addressable chunks

The shim maintains a per-session cache keyed by `sha256(compressed_chunk)[:8]`.
The first time a chunk appears it is rendered in full with a fingerprint header:

```
{@a4f9c1d2 = outline of config.py}
§1=a1b2c3d4  module app
§4=d4e5f6g7    class Config
§6=3f9a1c2d      def load(self) -> dict
```

On subsequent turns the same chunk is replaced by its reference:

```
{@a4f9c1d2}
```

The shim expands the reference on the way in — the model sees the full content.
The LoRA is trained to treat `{@HASH}` tokens as an already-seen expanded chunk.
On the way out, the shim strips `{@...}` tokens before returning to the client.

The cache is session-scoped and stored shim-side. No client changes required.

### Anaphoric references

Short-form aliases for common cross-turn references:

| Token | Meaning |
|-------|---------|
| `{@prev}` | Most recent expanded chunk |
| `{@target}` | Most recently addressed `§N` position |
| `{@last_edit}` | Code block from the last edit operation |
| `{@HASH}` | Specific named chunk by fingerprint |

The model emits anaphors in interaction commands and context requests:

```
expand {@target}       ← re-expand the current target
callers {@prev}        ← callers of the chunk just seen
```

### Cross-turn deltas

When a chunk is modified, the model can reference the original and describe
the delta rather than re-emitting the full chunk:

```
{@a4f9c1d2} + rationale §6: added empty-file guard
```

The shim stores the resolved pre-patch version as `@a4f9c1d2` and the
post-patch version under a new fingerprint.

### External mutation detection

A cached chunk becomes stale if the underlying file changes between turns
(e.g., git pull, IDE edit, CI pipeline write). The shim detects this by
re-fingerprinting the `CodeIndex` at the start of each turn and comparing
against the registered chunk hash. On mismatch:

1. The stale chunk is evicted from the session cache.
2. The new state is registered under a fresh fingerprint.
3. The current turn's context is assembled with the fresh fingerprint.
4. A `session_cache_staleness` metric is incremented in telemetry.

**Partial invalidation:** Only changed chunks are evicted. If `config.py`
changes but `auth.py` does not, the `auth.py` outline chunk remains cached.
Staleness detection is per-chunk, not per-session.

### Eviction policy

Session cache chunks are stored in an LRU structure scoped to the session.
- **Default maximum:** 512 chunks per session (configurable via
  `X-Ozma-Cache-Max-Chunks` header).
- **Eviction:** least-recently-used chunk is evicted when the limit is
  reached. The `@prev`, `@target`, `@last_edit` pointer targets are always
  pinned (never evicted).
- **Size cap:** individual chunks exceeding 64k tokens are not cached
  (rendered inline each turn instead).

#### Kind-aware eviction (v5.11 — `TOOL-RESULT-CACHE`)

Tool-heavy agent loops (grep dumps, file reads, search results) fill the
cache with high-recency high-volume content that a uniform LRU policy
evicts at the same rate as stable code-outline chunks. Code outlines are
long-lived reference material; tool results are near-term scratchpad. The
eviction policy should distinguish.

Every cached chunk carries a **kind tag**:

| Kind | Source | Typical size | Retention default |
|---|---|---|---|
| `code-outline` | OUTLINE / stubbed source | 500–5000 tok | High (reference material) |
| `tool-result` | Dispatch/INTERACT result, grep output, file read | 100–10000 tok | Recency-biased (scratchpad) |
| `reasoning` | REASONING-PERSIST `[prior reasoning]` | 50–800 tok | Pinned while goal active |
| `handoff-concluded` | HANDOFF `concluded:` block context | 200–2000 tok | Pinned for handoff receiver's first N turns |
| `memory` | MEMORY `remember:` resolved chunk | 50–500 tok | Pinned |
| `session-ref` | SESSION-REF resolved slice | 100–2000 tok | Standard LRU |
| `vision-frame` | VISION pixel reference | variable | Aggressive (pixels are expensive) |

**Policy knobs** (registry `cache_policy`):

```python
"cache_policy": {
    "tool_result_retention": "recent-N" | "recent-session" | "all",  # default "recent-session"
    "outline_eviction_bias": "aggressive" | "standard" | "retain",    # default "standard"
    "reasoning_chunk_evictable": false,                               # default false
    "tool_result_recent_n": 20,                                       # when retention=recent-N
}
```

**Tool-result retention modes:**

- `recent-N` — keep the last N tool-result chunks; older evict
  regardless of LRU order. Matches K2.6's *"discard old context, retain
  recent tool interactions"* strategy. Recommended N: 15–25 for
  single-agent loops; higher for multi-agent coordination.
- `recent-session` (default) — keep all tool-result chunks until session
  end. Suitable for short-to-medium sessions.
- `all` — no special handling; tool-result chunks are pure LRU. Matches
  pre-v5.11 behavior.

**Outline eviction bias:**

- `aggressive` — outline chunks evict at 0.5× LRU weight (faster).
  Appropriate when the agent is in a narrow focus area and far outlines
  are unlikely to re-reference.
- `standard` — outlines evict at normal LRU weight.
- `retain` — outlines evict at 2× LRU weight (slower). Appropriate for
  large-codebase exploratory sessions where any outline may re-surface.

**Pinning semantics.** `reasoning_chunk_evictable: false` pins the
REASONING-PERSIST `[prior reasoning]` block while its bound AGENT-STATE
goal is active. Pinned chunks do not count against the per-session chunk
cap (a separate `pinned_cap` applies, default 32).

**Kind-aware telemetry** per session:

```jsonc
{
  "cache_chunks_by_kind": {
    "code-outline": 142, "tool-result": 340, "reasoning": 18,
    "handoff-concluded": 4, "memory": 11, "session-ref": 6, "vision-frame": 2
  },
  "evictions_by_kind": {
    "code-outline": 3, "tool-result": 287, "reasoning": 0, ...
  },
  "tool_result_retention_hit_rate": 0.94   # fraction of @obs references finding cached result
}
```

**Interaction with THINK-ACT.** Observations produced by `interact:`
commands inside THINK_ACT_LIST are cached as `tool-result` kind and
`@obs:N` anaphors are tool-result-retention-aware. Out-of-scope
observation references may be rescued from cache if still resident.

**Prose fallback:** Kind tags are emitted alongside `{@HASH}` but unused by
untrained consumers. Eviction falls back to uniform LRU.

### Cross-session persistence

`sha256(compressed_chunk)` is content-addressed and stable across shim
restarts. If a deployment persists the cache store (e.g., Redis), the same
chunk hash from a prior session is immediately valid in a new session —
no re-upload needed. The cache entry carries a `created_at` timestamp;
entries older than `cache_ttl` (default 7 days) are treated as expired
and re-fetched. Expiry is configurable per deployment.

### Cross-model identity within a session

Session cache chunks remain valid when the model serving the session changes
mid-session (e.g., turn 1–5 served by model A, turn 6 onward by model B).
Cache entries are keyed by content hash, not model identity. The new model
receives the same `{@HASH}` references in its context and the shim expands
them identically. The protocol version and capability set for the session
are locked at the first ack — if the new model acks different capabilities,
the shim uses the intersection of both ack sets for the remainder of the
session, logging `model_switch_cap_delta` for any capability that changed.
The new model does NOT re-emit an ack header; ack is once-per-session.

**Anti-spoof guarantee scope:** The HMAC in the ack proves the model is the
trained model — but only for the first model. If the orchestrator switches to
model B mid-session, model B never acks, so the shim cannot verify model B's
identity. The anti-spoof guarantee established in the first ack does not
extend past the first-ack'd model. Orchestrators requiring per-model
authentication SHOULD trigger an out-of-band silent probe/ack exchange before
passing any turn to the new model (not shown in the conversation thread).

For security-sensitive deployments, set `"strict_antispoof": true` in the
registry entry. When enabled, the shim MUST complete a silent probe/ack
exchange with any new model before routing turns to it. Turns routed before
the exchange completes are refused with `silent_ack_required` logged. The
default is `false` (advisory-only, SHOULD).

**Persistent state across model switches:** Memories written by model A
(`remember:` blocks) persist in the session and workspace stores across model
switches. The shim will inject them as usual in subsequent turns. If model B
lacks the MEMORY capability (not in its registry caps), injected memories
appear as plain text in the user-turn prefix and are not parseable by model B
as structured memory entries. Shims SHOULD log a `model_switch_cap_delta`
event when the incoming model's capability set differs from the departing
model's, so the orchestrator can warn if critical capabilities (e.g., MEMORY,
FORMAL-CLAIM) are lost mid-session.

### Expected cost impact

On a 50-turn agent session, chunks after the first turn cost zero context
tokens. Estimated **2–4× cost reduction** on top of single-turn compression,
compounding with minification.

### Grammar (illustrative)

```
HASH_REF    = "{@" , ( STABLE_ID | "prev" | "target" | "last_edit" ) , "}" ;
CHUNK_HDR   = "{@" , STABLE_ID , " = " , PROSE , "}" , NEWLINE ;
```

### Shim responsibilities

- Assign fingerprints to all compressed chunks on first render.
- Store `session_id → {hash → chunk}` in memory for session lifetime.
- Replace repeated identical chunks with `{@HASH}` on assembly.
- Expand all `{@...}` tokens before the content reaches the model.
- Strip `{@...}` tokens from any model output before returning to client.
- Maintain `@prev`, `@target`, `@last_edit` pointers updated each turn.

### Cross-Session References (v5.9.2 — `SESSION-REF`)

Session cache refs extend across sessions for users and workspaces that
have the `SESSION-REF` capability enabled. Solves the most common
workflow pain: *"check the other session for details"* should just work
without manual copy-paste.

**Reference forms** — `session` namespace in the unified REF table:

| Form | Resolves to |
|---|---|
| `{@session:sess_abc123}` | Whole session; shim picks relevant slice via EMBED semantic retrieval |
| `{@session:django-migration}` | Session by user-assigned label |
| `{@session:sess_abc123/turn:17}` | Turn 17 of that session |
| `{@session:django-migration#rollback-plan}` | Tagged span within a labeled session |
| `{@session:prev}` | Last-active session before the current one (anaphoric) |
| `{@session:current}` | This session (for self-reference in meta-ops) |

**Natural-language bridge (three layers).** The primitive is what the
model emits; the user always speaks naturally. The three layers that
make *"check the other session"* work without anyone typing a ref:

1. **Shim pre-injects `[session-registry]`** at turn start (when
   `SESSION-REF` is enabled). Recent sessions appear as ambient context
   — id, label, last-active, one-line summary, unresolved-action flag.
   ~100–200 tokens; the model sees recent sessions without having to
   ask.
   
   ```
   [session-registry mode=summary scope=user]
     recent:
       - { id: sess_a4c2, label: django-migration, last_active: -2h,
           summary: "debugged rollback; ended mid-task on migration 0042",
           has_unresolved: true }
       - { id: sess_9b8f, label: oauth-redesign, last_active: -1d,
           summary: "completed design doc; handed off to MOB-487",
           has_unresolved: false }
       - { id: sess_3e1d, label: null, last_active: -3d,
           summary: "small pandas-to-polars refactor",
           has_unresolved: false }
   [/session-registry]
   ```

2. **Hint fires on trigger phrases.** A rules-engine entry in
   `ozmalib/rules/data/session-ref-natural-language.yaml` detects user
   utterances matching any of:
   
   - `the other session`, `other one`
   - `yesterday's chat`, `last night's session`, `earlier today`
   - `like we discussed earlier`, `as mentioned before`
   - `previous conversation`, `last conversation`, `last time`
   - `the one where we...`
   
   On match, shim injects a targeted reminder naming the best-matching
   candidate sessions (by recency and semantic similarity to the
   surrounding turn), plus a one-line teaching prompt showing the
   resolution form.

3. **Teaching prompt for untrained models.** Models without `SESSION-
   REF` trained natively receive (via v5.3 self-upgrade) a compact
   teaching prompt showing natural-language → primitive mapping
   examples. Enables instruction-tier-quality emission.

Net user experience: *"check the other session for details"* → model
emits `[session:search query="..."]` or resolves a `{@session:...}`
ref from the ambient registry → shim retrieves relevant chunks →
context appears. The user never sees the machine form.

**Relevance extraction, not dump.** When a session ref resolves, the
shim does not copy the entire session history into context. The
existing EMBED machinery (v2.0) extracts the semantic slice most
relevant to the referring turn. Content-addressable chunks
deduplicate naturally — a chunk present in both sessions is stored
once.

**Labels and sharing.**

```
[session:label "django-migration"]                  # label current
[session:label for={@session:sess_a4c2} "billing-refactor"]

[session:share {@session:django-migration}
               with=user:"priya@example.com"
               read_only=true]
[session:share {@session:sprint-retro}
               with=workspace:"platform-team"]
```

Labels become valid `{@session:<label>}` REF targets. Shares produce a
TRACE entry and are revocable.

**Search primitive.**

```
[session:search scope=user limit=3 since=-30d]
  query: "the one where we debugged the migration rollback"
[/session:search]

# shim responds inline (or as a follow-up input block):

[session:search-response]
  matches:
    - { id: sess_a4c2, label: django-migration, relevance: 0.94,
        last_active: -2h,
        snippet: "...the rollback failed because constraint X..." }
    - { id: sess_7e3b, label: null, relevance: 0.71,
        last_active: -14d,
        snippet: "...different migration; postgres rollback..." }
[/session:search-response]
```

**Privacy scope (normative):**

- Sessions are user-scoped by default. One user's sessions cannot be
  referenced from another user's sessions without explicit
  `[session:share]`.
- Deleted sessions: ref resolution returns `session_deleted` error.
- Sensitive sessions (auto-flagged by `contains-secret` compliance
  tag propagation from v5.9.1, or user-flagged via
  `[session:mark-sensitive]`) cannot be referenced except from within
  the same session.
- Cross-workspace refs require `X-Ozma-Session-Scope: global`;
  deployments can forbid globally.
- Shim always enforces the session-ACL check before returning any
  chunk via cross-session resolution; a failed check returns
  `session_share_denied` without revealing whether the session
  exists.

**Composition with v5.9 COORDINATION.**

Session subscriptions reuse coordination machinery to watch another
session's state changes:

```
[subscribe scope=session ref={@session:sess_a4c2}
           events=[dispatch_complete, work_unit_status_change, handoff]
           as=agent:current]
```

Use case: *"let me know when that long-running dispatch in the
other session finishes"* — natural-language utterance, model emits
the subscription, agent turn fires automatically when the event
arrives (with appropriate wake mode per v5.9 agent wake policy).

**What this unlocks.**

- **Threaded work across days.** Tuesday's session referenced from
  Thursday without manual recap.
- **Model-initiated lookup.** *"I recall we discussed this — let me
  check"* → agent emits `[session:search]` autonomously, reads its
  own prior history to avoid relitigating settled questions.
- **Session-as-citation** in WORK-UNIT status updates, design
  decisions, review messages: *"per @session:architecture-review
  we chose the bridge approach"*.
- **Cross-agent session sharing.** Agent A can reference agent B's
  sessions (within the same workspace or with explicit share)
  without a heavyweight v1.2 HANDOFF.

**Implementation cost (shim side):**

- Session storage already exists; add a cross-session index keyed
  by user × workspace × label
- Semantic retrieval reuses EMBED
- Privacy scope reuses workspace memory ACL (v1.5 + v2.0)
- Registry injection piggy-backs on the turn-start context assembly
- Hint entry is one YAML file
- Net: low hundreds of lines of shim code

---

## Pattern Compression

For repetitive code (route handlers, CRUD methods, test suites), cross-function
structural deduplication produces 5–20× compression on top of tree-sitter
stubbing.

### Repetition extraction

When the stub pass identifies N ≥ 3 functions with identical structure, the
shim renders them as a named pattern.

**"Identical structure"** is defined as: the tree-sitter node-type sequence
of the function body (post-stub) matches exactly, with only
leaf-node identifier values differing. The comparison is on AST node types,
not source text. Two functions that differ only in variable names and string
literals qualify; two functions with different control-flow shapes do not.
The threshold N is configurable (default 3); the similarity check is exact
(no fuzzy matching) to prevent miscoded pattern edits.

```
pattern P1: ƒ handle_{name}({arg}: {T}) → Response: ...
  §12=aa1b  handle_user(user: User)
  §13=bb2c  handle_post(post: Post)
  §14=cc3d  handle_comment(comment: Comment)
  ... 17 more (§15–§31)
```

`{name}`, `{arg}`, `{T}` are positional slots. The pattern body uses `...`
for the shared implementation. Individual entries show only the differing
slot values and their `§N=ID` addresses.

The model edits a pattern by addressing the pattern name (all members) or
a comma-separated subset (partial-pattern edit):

```
edit P1: body
```python
ƒ handle_{name}({arg}: {T}) → Response:
    → JSONResponse(content={arg}.model_dump())
```
```

This applies the edit to all members. To edit only a subset:

```
edit P1:§13,§14: body
```python
ƒ handle_{name}({arg}: {T}) → Response:
    → JSONResponse(content={arg}.model_dump(), status_code=201)
```
```

This applies the edit to `handle_post` and `handle_comment` only, leaving
other members unchanged. Alternatively, addressing `§13` directly edits
only `handle_post` and removes it from the pattern.

**`pattern_ambiguous` error:** Pattern detection is exact-match only — a
pattern is formed only when N ≥ 3 members have an identical tree-sitter
node-type sequence. The `pattern_ambiguous` error fires not during detection
(which is exact) but during *application*: if `edit P1: body` is issued
after one or more pattern members have been independently edited since the
pattern was formed, those members no longer share the original shape. The
shim cannot safely apply the pattern edit to a heterogeneous member list.
Resolution: address modified members individually (`edit §13:`, `edit §14:`),
then re-issue the pattern edit for the remaining uniform members. The shim
logs which members diverged in the error payload.

### Cross-file structural sharing

If `config.py`'s imports are already shown in the session cache (`{@HASH}`),
`main.py`'s outline that imports from config references the cached chunk
instead of re-listing symbols. The shim detects this via the `CodeIndex`
dependency graph and emits `← {@HASH}` in the outline entry.

---

## Streaming Invariants

The shim supports streaming via `expand_stream_delta`. Each data type has
specific invariants for correct operation in streaming mode.

**Ack header:** The shim scans the first 3 non-blank lines of the first turn.
In streaming mode, the shim buffers chunks until it has accumulated 3 non-blank
lines or 512 bytes (whichever comes first), then makes the ack determination.
If the ack is present, the shim withholds those buffered chunks from the client
until the ack line is stripped. **Level confirmation is deferred** until the
buffer threshold is reached — callers must not rely on level being confirmed
before the first client-visible chunk.

**`{@HASH}` references mid-stream:** The shim buffers from `{@` until the
closing `}` before expanding. If the chunk boundary falls inside the reference
(e.g., `{@a4f9` in one chunk and `c1d2}` in the next), buffering handles this
transparently. Maximum reference length is 12 bytes; a `{@` not closed within
12 bytes is passed through unexpanded.

**TRANSACTION across chunks:** The shim buffers the entire transaction (from
`transaction` to `end T1`) before validating or applying. Streaming clients
see no output from a transaction until `end T1` arrives. On timeout (default
30s without `end TN` arriving), the transaction is aborted with
`transaction_timeout`.

**Circuit-breaker mid-stream:** If a structured output type's parse fails
while the response is still streaming, the shim allows the current response
to complete (does not cut the stream), increments the failure counter, and
disables the capability for subsequent turns. The client receives the
`X-Ozma-Disabled` header on the *next* response, not the current one.

**MEMORY `remember:` blocks in stream:** By default, `remember:` operations
are buffered and committed only on successful turn completion. `remember:immediate`
commits as chunks arrive. If a stream is interrupted (`stream_interrupted`) after
a `remember:immediate` line, that entry persists; non-immediate `remember:` entries
buffered in the same turn are discarded.

**`recall` in stream:** `recall` is a terminal command. When the shim detects
a `recall "..."` line in the streaming output, it allows the current line to
complete, then terminates the stream (no further model output is consumed). The
shim delivers the recall result in the next user turn. Constrained decoders
must halt token generation on detection of `recall` at a turn-start-eligible
position (after BODY and optional MEMORY_CMD entries).

---

## Memory Graph Operations

### Spreading Activation

The shim retrieves memories via spreading activation over the graph. This
replaces any flat-key-value retrieval.

**Parameters:**

| Parameter | Default | Config header | Range |
|---|---|---|---|
| `max_hops` | 2 | `X-Ozma-Memory-Hops` | 1–5 |
| `activation_tolerance` | 1e-6 | — | (L1 norm convergence threshold) |
| `seed_count` | 20 | `X-Ozma-Memory-Seeds` | 5–100 |
| `result_token_budget` | 600 | — | per injection budget |

**Activation iteration:**

```
a_{t+1}(m) = A · a_t(m) + B · b(m)
```

Where:
- `a_t(m)` is activation at node m at iteration t
- `A` is the weighted adjacency matrix (edge strength × edge-type decay β)
- `b(m)` is source activation from embedding-matched seed nodes
- `B` is the source injection matrix

The shim iterates until `||a_{t+1} − a_t||_1 < activation_tolerance` or
`max_hops` iterations, whichever comes first.

**Edge-type decay factors (β):** the shim applies these when building A:

| Edge | β |
|---|---|
| `confirms` | 0.9 |
| `tested_by` | 0.9 |
| `contradicts` | 0.8 |
| `depends_on` | 0.7 |
| `supersedes` | 0.6 |
| `generalises` | 0.5 |
| `instance_of` | 0.5 |
| `authored_by` | 0.4 |

Strength overrides multiply the edge-type β: effective weight = strength × β.
The shim MUST reject a `strength` value that would cause any cycle's effective
weight product to exceed 1.0. If this would happen, the shim clamps the
conflicting edge strength to `1.0 / (product_of_other βs in cycle)`.

**Retrieval phases:**

1. Seed selection: embedding similarity against task summary + target positions.
   Return top-K seed nodes (k = `seed_count`).
2. Traversal expansion: for each seed, walk edges outward up to `max_hops`.
   Activation accumulates at each hop, weighted by effective edge weight.
3. Return nodes by final activation score, capped at `result_token_budget`.

**Convergence check:** If any cycle's effective weight product ≥ 1.0, the
shim breaks the cycle at the weakest edge (lowest effective weight) before
running activation. This is a pre-processing step; it modifies the graph
structure for this activation pass only and does not persist.

### Consolidation Cycle

Consolidation runs during idle periods and after `consolidate:` commands.
It assesses the trace chain, updates node state, and enforces invariants.

**Consolidation phases:**

1. **Trace assessment.** Read the trace chain for the consolidation window
   (default: traces since last consolidation). For each reasoning step,
   classify as `successful` (output accepted) or `failed` (output rejected /
   scope-check failure). A memory node's valence is incremented for each
   successful step in which it was activated, and decremented for each failed
   step in which it was activated. Clamp to [-1, 1].

2. **Activation count update.** For each memory node that appeared in a
   retrieval result since the last consolidation, increment `activation_count`
   by 1.

3. **Co-activation edge creation.** For pairs of memory nodes that co-
   appeared in the same retrieval result (same turn, both in top-K),
   the shim MAY add a `co_activated_with` edge (strength = co-occurrence
   frequency / total retrievals). This is an internal inference edge;
   it does not appear in model-authored edge lists but does participate
   in future spreading activation. Shims MAY disable this step if
   `consolidation_co_activation: false`.

4. **Low-activation pruning.** Nodes with `activation_count == 0` and no
   incoming edges from non-memory elements are pruned. Nodes with
   `activation_count < threshold` (default 2) and no incoming edges are
   flagged `[low-activation]` but not automatically pruned.

5. **Ungrounded pruning.** A node is *grounded* if it has at least one
   incoming or outgoing semantic edge to a non-memory element. Memory-to-
   memory edges alone do NOT establish grounding. Nodes that are not
   grounded AND have `activation_count == 0` are pruned.

6. **Temporal edge preservation.** `caused_by` and `preceded_by` edges
   between surviving memory nodes are preserved — they record the causal
   ordering of the reasoning trace and are not a form of inconsistency.

**Idle-cycle trigger:** The shim MAY run consolidation on idle (no active
turns for >5 minutes). It MUST NOT run consolidation while a turn is in
progress. Configurable via `consolidation_idle_enabled: true/false`.

**Grounding invariant:** After every consolidation phase 4 and 5, the
shim MUST verify that all surviving memory nodes are grounded. If a pruning
decision would leave a surviving node ungrounded, the shim skips that
pruning decision. The invariant holds: every surviving memory node has at
least one path to a non-memory element.

### Trace Backend

**Trace entry storage:** The shim stores trace entries in the trace backend
(adapter kind `trace-backend`, configured via `X-Ozma-Trace-Adapter`). Each
entry is a JSON object:

```json
{
  "trace_id": "t:7fa1d8e2c4b93601",
  "step": 3,
  "task": "Fix Config.load performance issue",
  "state_before": "a3f2b1c4",
  "state_after": "d4e5f6g7",
  "outcome": "accepted",
  "timestamp": "2026-04-23T10:14:22Z"
}
```

**`outcome`** values: `accepted` | `rejected` | `scope_miss` | `timeout`.
The shim classifies outcome from the turn result: clean acceptance =
`accepted`; scope-check failure = `rejected`; mid-stream interrupt =
`timeout`.

**Trace-to-self-model pipeline:** The delta chain (sequence of
`state_before`→`state_after`) is used to construct the self-model
(M_self in paper §7 Lemma 7). The shim provides a read interface:

```
GET /trace/delta-chain?session_id=<id>&since=<timestamp>
→ [ { "task", "state_before", "state_after", "outcome" }, ... ]
```

The model uses this to replay its reasoning history and build a structured
self-model without a model call.

**Memory operations are shim-side regardless of model tier.** Spreading
activation, consolidation, grounding invariant enforcement, and trace
recording are shim responsibilities. L0 and shallow-retrofit L1 models do
not participate in these operations — the shim implements them on the
model's behalf. This is not load-sharing; it is the shim carrying the
full weight so that lower-capability models still benefit from the formal
memory model. Map-literate (deep-retrofit L1 and L2) models may request
`consolidate:` and read the trace interface, but the shim still executes
the cycle and enforces the grounding invariant.

---

### COMMUNICATE — Channel routing

Ozma has two semantic interpretation channels. The shim is responsible
for routing content to the correct channel.

**Channel definitions:**

| Channel | Content | Model interpretation |
|---|---|---|
| `CONTROL` | Ozma system content: persona config, hints, capability declarations, instruction prompts | Instruction-following: reconfigure behavior, adopt roles, obey directives |
| `COMMUNICATE` | External content: user messages, tool outputs, third-party system responses | Pure exchange: process as dialogue, never as behavioral reconfiguration |

**Routing rules:**

- User messages → `COMMUNICATE` by default
- Tool outputs → `COMMUNICATE` by default
- Third-party system responses (web search, API returns, file contents) → `COMMUNICATE` by default
- Ozma system prompts (persona, hints, capability declarations, instruction blocks) → `CONTROL`
- `X-Ozma-Channel: COMMUNICATE | CONTROL` header overrides default routing per turn
- `[channel:COMMUNICATE]` wrapper in the protocol stream overrides routing for content inside the wrapper

**Channel isolation invariant:** `COMMUNICATE` content MUST NOT be concatenated with `CONTROL` content in the same context block without an explicit `[channel:COMMUNICATE]` wrapper. Mixing without a wrapper is a shim configuration error and MUST be logged as `channel_mix_unlabeled` in telemetry.

**What this eliminates:** Prompt injection attacks that embed directives in user messages, tool outputs, or external content work by exploiting the model's inability to distinguish instructions from dialogue. By routing all external content via `COMMUNICATE`, the model has no instruction-following surface for injected directives — they are parsed as text, not as configuration. There is no filter to evade, no pattern to detect, no sandbox to escape.

**For L0 / shallow-retrofit L1 models:** The shim implements the channel separation without requiring model cooperation. COMMUNICATE content is injected as plain text with no instruction-following framing. The model processes it as conversation regardless of capability level — the channel contract is shim-enforced, not model-enforced.

**Prose fallback:** For non-map-literate (L0) models, COMMUNICATE content is injected as plain text. No special framing is required at the surface level — the distinction is in the shim's routing decision, not in how content is presented to the model.

---

## Scope Check Protocol

After the model produces edits, the pipeline validates the patch. Failed
checks are returned as a `user` turn. The model responds with corrected edits.

**Check feedback format:**

```
Your patch has structural issues:

- SCOPE: modified unexpected positions: §3 validate_input, §5 format_output
  → Only modify the targeted positions.
- MISSED: expected targets not modified: §6 Config.load
  → This target should have been modified.
- API BREAK: §8 parse_args: signature changed — API break?
  → Changing signatures breaks callers. Keep the original signature.

Passed: 2 positions changed

Please revise your patch addressing the issues above.
```

### Capability circuit-breaker

If a structured output type (RATIONALE, HINT, OUTLINE, GRAPH) produces
parse failures on N consecutive turns (default N=3), the shim disables that
capability for the remainder of the session and logs `cap_disabled`. Other
capabilities remain active. The client receives `X-Ozma-Disabled: RATIONALE`
in the response headers.

**"Consecutive" definition:** A failure turn is any turn where the capability
was expected (the capability is active and the context package contained
relevant content) but the output failed to parse. A successful parse resets
the counter to zero. Turns where the capability was not exercised — neither
expected nor emitted — do not count for or against the counter.

**Correction retries:** A correction-turn retry (the model receives scope-check
feedback and responds) counts as a new turn. If the retry also fails to produce
valid structured output, it counts as a consecutive failure.

**Persistence:** Circuit-breaker state is session-scoped. It is not persisted
past session end. A new session starts with all capabilities active regardless
of prior session behaviour.

**Re-enable:** A client may re-enable a disabled capability mid-session via
`X-Ozma-Caps: RATIONALE` on a subsequent request. This resets the counter to
zero. Useful when a client knows the model mis-fired (e.g., a prompt-injected
string triggered the parse failure). Also re-enabled automatically if the
`X-Ozma-Caps` header explicitly lists the capability.

---

## Context Assembly Order

```
[System message — stable, cache-friendly]
  Ozma probe line                          ← retrofit-trained L1 or L2 only
  Protocol instruction                     ← full-retrofit L1 or L2 only

[User-role prefix block — per-request, dynamic]
  <legend>a1=long_name_1, a2=long_name_2</legend>   ← when minification active

[User message]
  Context: {issue summary}
  Likely targets ({N}):
    §6=3f9a1c2d method Config.load L34-42 (reason)
    ...

  --- Code outline ---
  §1=a1b2c3d4  module app
  §4=d4e5f6g7    class Config
  §6=3f9a1c2d      def load(self) -> dict
  ...

  --- Target code ---
  # §6=3f9a1c2d method Config.load (L34-42)
  ƒ load(α) → dict:
    ...

  --- Call graph ---
  §6 Config.load: callers: parse_args, main; callees: open, json.load

  --- Test coverage ---
  Tests covering targets:
    test_config.py:test_load_empty_file → tests load
    test_config.py:test_load_valid → tests load

  --- Git context ---
  config.py L34-42 — last changed 3 days ago by alice
  Uncommitted: +2 lines in Config.load

  --- Constraints ---
  Modify only the targeted positions:
    §6 Config.load (empty file crash)
  Do not modify other functions unless necessary.
```

---

## Shim Responsibilities by Level

Columns describe the retrofit depth inside L1 plus L2 native. L0 = stock;
L1 `prompted` = pure prompting; L1 `partial` = shallow retrofit; L1
`protocol_lora` = deep retrofit; L2 = native map generation.

| Responsibility | L0 | L1 prompted | L1 partial | L1 protocol_lora+ / L2 |
|---|---|---|---|---|
| Look up model registry | ✓ | ✓ | ✓ | ✓ |
| Inject probe | — | — | ✓ | ✓ |
| Try parse ack (all levels, advisory) | ✓ | ✓ | ✓ | ✓ |
| Verify ack hmac | — | — | ✓ | ✓ |
| Strip ack from response | — | — | ✓ | ✓ |
| Validate alias declarations | — | — | ✓ | ✓ |
| Compress outline (input) | — | ✓ | ✓ | ✓ |
| Compress graph/tree (input) | — | ✓ | ✓ | ✓ |
| Compress logs/traces (input) | — | ✓ | ✓ | ✓ |
| Pattern compression (repetitive functions) | — | ✓ | ✓ | ✓ |
| Maintain session cache (fingerprint chunks) | — | ✓ | ✓ | ✓ |
| Replace repeated chunks with `{@HASH}` | — | ✓ | ✓ | ✓ |
| Expand `{@HASH}` before model sees context | — | ✓ | ✓ | ✓ |
| Maintain `@prev`/`@target`/`@last_edit` refs | — | ✓ | ✓ | ✓ |
| Apply `preview` without mutating working tree | — | ✓ | ✓ | ✓ |
| Execute `search semantic` via vector index | — | ✓ | ✓ | ✓ |
| Apply TRANSACTION atomically, rollback on fail | — | — | ✓ | ✓ |
| Inject wire instructions | — | — | — | ✓ |  # cap-gated: if WIRE in trained_caps
| Expand wire prose (output) | — | — | — | ✓ |  # cap-gated: if WIRE in trained_caps
| Validate wire table version | — | — | — | ✓ |  # cap-gated: if WIRE in trained_caps
| Validate rationale vs actual diff | — | — | — | ✓ |
| Validate outline output | — | — | — | ✓ |
| Parse hint output | — | — | — | ✓ |
| Parse claim output, store + trigger verification | — | — | — | ✓ |
| Strip reasoning block, store in telemetry | — | — | — | ✓ |
| Execute plan steps, report step-level failures | — | — | — | ✓ |
| Relay test-spec to test-gen orchestrator | — | — | — | ✓ |
| Process HANDOFF in/out for multi-model routing | — | — | — | ✓ |
| Circuit-break failed capabilities | — | — | — | ✓ |
| Store `remember:` blocks to memory store | — | ✓ | ✓ | ✓ |
| Respond to `recall` queries from memory store | — | ✓ | ✓ | ✓ |
| Inject relevant workspace/user memories at turn start | — | ✓ | ✓ | ✓ |
| Expire and LRU-evict memory store entries | — | ✓ | ✓ | ✓ |
| Inject persona instructions in system prompt | — | ✓ | ✓ | ✓ |
| Respond to `persona?` queries | — | — | ✓ | ✓ |
| Process `persona:` switch requests (if allowed) | — | — | — | ✓ |
| Tag all telemetry with active persona | ✓ | ✓ | ✓ | ✓ |
| Emit telemetry events | ✓ | ✓ | ✓ | ✓ |
| Analyse CodeIndex identifier frequency for tokenizer-layer map | — | ✓ | ✓ | ✓ |
| Persist and load tokenizer-layer map alongside workspace memories | — | ✓ | ✓ | ✓ |
| Record attention signal per turn | — | ✓ | ✓ | ✓ |
| Expand attended positions adaptively (ATTN) | — | ✓ | ✓ | ✓ |
| Emit DOCSTUB `[doc: N tokens "..."]` placeholders in stubs | — | ✓ | ✓ | ✓ |
| Serve `expand doc §N` responses | — | ✓ | ✓ | ✓ |
| Register embed aliases from `search semantic ... as embed:e1` | — | ✓ | ✓ | ✓ |
| Resolve EMBED_REF `{@embed:id}` to position in EDIT/INTERACT | — | ✓ | ✓ | ✓ |
| Enforce reserved embed alias names (prev/target/last_edit) | — | ✓ | ✓ | ✓ |
| Parse STRUCTURED-CONF maps; apply primary-aspect min gate | — | — | — | ✓ |
| Pass FORMAL-CLAIM to registered verifier; record verify_status | — | — | — | ✓ |
| Return refuted FORMAL-CLAIM counterexample in normative format | — | — | — | ✓ |
| Store and inject POLYGLOT map in session cache | — | ✓ | ✓ | ✓ |
| Inject POLYGLOT map as `[polyglot context]` prefix block | — | ✓ | ✓ | ✓ |
| Validate polyglot identifiers against CodeIndex | — | — | — | ✓ |
| Apply POLYGLOT newest-wins conflict resolution | — | ✓ | ✓ | ✓ |
| Record COLLAB weights in telemetry; pass to training pipeline | — | — | ✓ | ✓ |
| Apply COLLAB calibration factor from deployment config | — | — | — | ✓ |
| Record FEDERATED annotations; send to aggregator in HANDOFF | — | — | — | ✓ |
| Track DP epsilon/delta budget; exclude over-budget turns | — | — | — | ✓ |
| Detect model switch; lock capability set to first-ack intersection | — | ✓ | ✓ | ✓ |
| Enforce silent probe/ack on model switch when `strict_antispoof: true` | — | — | ✓ | ✓ |
| Apply workspace content filter to all workspace writes | — | ✓ | ✓ | ✓ |
| Version and log memory content-filter decisions | — | ✓ | ✓ | ✓ |
| Enforce `X-Ozma-Workspace` ACL in multi-tenant deployments | — | ✓ | ✓ | ✓ |
| Tag injected memories older than 90 days with `[old]` | — | ✓ | ✓ | ✓ |
| Normalize LaTeX math input to canonical Unicode (MATH) | — | — | — | ✓ |
| Resolve `{@math:EN}` cross-turn references in input | — | — | — | ✓ |
| Validate TENSOR-SHAPE against language-specific shape inference | — | — | — | ✓ |
| Validate SCHEMA compat tag against prior cached schema version | — | — | — | ✓ |
| Auto-populate DEP CVE notes from CVE monitor | — | — | — | ✓ |
| Run EFFECTS static analysis and record verify status | — | — | — | ✓ |
| Run PROMPT injection-resistance and token-budget verifiers | — | — | — | ✓ |
| Persist AGENT-STATE keyed by session_id; inject as prefix block | — | — | — | ✓ |
| Log `agent_goal_complete` when AGENT-STATE progress = 1.0 | — | — | — | ✓ |
| Resolve STYLE (named lookup / inline block / persona context-style map) | — | ✓ | ✓ | ✓ |
| Render THEME per `X-Ozma-Render` target; strip metadata for `plain` | — | ✓ | ✓ | ✓ |
| Translate Ozma-native PROSODY to target TTS flavor via `tts_adapters/*.yaml` | — | — | — | ✓ |
| Resolve VOICE to engine-specific voice ID per `X-Ozma-TTS` | — | — | — | ✓ |
| Adjust output emoji density based on THEME/Render capability | — | ✓ | ✓ | ✓ |
| Apply emotion-classifier-driven PROSODY defaults (rules-social.md §6) | — | — | — | ✓ |
| Enforce a11y capability constraints on THEME/PROSODY (§rules-social.md §14c) | — | ✓ | ✓ | ✓ |
| Resolve AVATAR to engine-specific identifier per `X-Ozma-Avatar` | — | ✓ | ✓ | ✓ |
| Load DEVICE profiles from `ozmalib/devices/*.yaml` | — | ✓ | ✓ | ✓ |
| Validate MOTION commands against device safety envelope before dispatch | — | ✓ | ✓ | ✓ |
| Route MOTION commands via transport adapters (ROS 2 / OPC UA / DDS / VMC-OSC / Autoware / MAVLink / MQTT / REST / vendor SDK) | — | ✓ | ✓ | ✓ |
| Gate `requires_confirm` actions via `needs_confirm` response; await client approve/deny | — | ✓ | ✓ | ✓ |
| Dispatch `motion [emergency-stop]:` to all registered devices at safety-critical priority | — | ✓ | ✓ | ✓ |
| Detect natural-language emergency-stop safe phrases; treat as `motion [emergency-stop]:` | — | ✓ | ✓ | ✓ |
| Maintain motion-audit log (separate from telemetry) | — | ✓ | ✓ | ✓ |
| Honor `X-Ozma-Motion: preview` — return trajectory from device planner, do not dispatch | — | ✓ | ✓ | ✓ |
| Honor `X-Ozma-Motion: dry-run` — validate against safety envelope, return result, do not plan or dispatch | — | ✓ | ✓ | ✓ |
| Apply emotion-classifier-driven MOTION defaults (avatar only — rules-social.md §6) | — | — | — | ✓ |
| Load sensor profiles from `ozmalib/sensors/*.yaml`; run declared pipelines | — | ✓ | ✓ | ✓ |
| Normalize VISION pipeline output to structured-perception schema (COCO-aligned) | — | ✓ | ✓ | ✓ |
| Compose multi-stage VISION pipelines (YOLO → SAM → MediaPipe → VLM) | — | — | ✓ | ✓ |
| Apply VISION format tier per `X-Ozma-Vision-Tier` (structured-only/reference/inline-thumb/inline-full/raw-stream) | — | ✓ | ✓ | ✓ |
| Transcode image formats (AVIF ↔ WebP ↔ JPEG ↔ raw) at client boundary | — | ✓ | ✓ | ✓ |
| Transcode video formats (AV1 ↔ H.265 ↔ H.264) at client boundary | — | ✓ | ✓ | ✓ |
| Render VISION-ANNOTATE per mode (overlay-svg / overlay-native / burn-in / structured-only) | — | ✓ | ✓ | ✓ |
| Execute VISION-EDIT operations; maintain content-addressable image cache | — | — | — | ✓ |
| Apply VISION-EDIT TRANSACTION atomically (all-or-nothing commit) | — | — | — | ✓ |
| Stream VISION-EDIT preview frames during long-running diffusion edits | — | — | — | ✓ |
| Parse `[specialist-dispatch]` / named forms emitted by model; resolve target from registry `good_at` tags | — | — | ✓ | ✓ |
| Execute SPECIALIST-DISPATCH synchronously; stream `[specialist-stream]` frames inline; commit `[specialist-result]` at completion | — | — | ✓ | ✓ |
| Apply SPECIALIST-DISPATCH cascade strategy (escalate on `confidence_below` or custom condition) | — | — | ✓ | ✓ |
| Apply SPECIALIST-DISPATCH race/vote/blend strategies (parallel specialist model calls) | — | — | — | ✓ |
| Include SPECIALIST-DISPATCH entries (with `kind` field) in `[rollup-ledger]` | — | — | ✓ | ✓ |
| Enforce kind-appropriate privacy envelope on artifact sent to dispatched specialist (face blur / no voice-fingerprint / PHI gating) | — | — | ✓ | ✓ |
| Apply deployment-default privacy policy for artifacts with no originating sensor profile | ✓ | ✓ | ✓ | ✓ |
| Handle `supplemental:` artifact list (all kinds; multi-artifact dispatch) | — | — | ✓ | ✓ |
| Route `kind=custom` dispatch via `good_at` tag intersection; no schema enforcement on result | — | — | ✓ | ✓ |
| Parse and propagate `[specialist-dispatch:cancel id=sd:N]`; abort in-flight specialist call and all descendants | — | — | ✓ | ✓ |
| Run LISTEN pipeline (Whisper/VAD/diarization/prosody/emotion/sound-event) | — | ✓ | ✓ | ✓ |
| Emit LISTEN utterances on utterance_end, silence threshold, or sound event | — | ✓ | ✓ | ✓ |
| Enforce voice-fingerprint prohibition (biometric embeddings never shipped) | ✓ | ✓ | ✓ | ✓ |
| Run SENSOR pipelines (IMU/GPS/depth/thermal/bio) with declared thresholds | — | ✓ | ✓ | ✓ |
| Enforce SENSOR privacy envelope (coarse location default; PHI gating) | ✓ | ✓ | ✓ | ✓ |
| Parse `[capability-probe]` emitted by model; forward to client; record ack | — | — | — | ✓ |
| Apply probe-accepted capabilities to remainder of session | — | — | — | ✓ |
| Load shim profile (`ozmalib/shim_profile.yaml`); advertise effective capabilities | ✓ | ✓ | ✓ | ✓ |
| Compute `effective = client ∪ {x \| shim_transform(x → y) ∧ y ∈ client}` | ✓ | ✓ | ✓ | ✓ |
| Apply declared transform bridges (html→image / svg→raster / md→html / vrm→video / ssml→mp3) | — | ✓ | ✓ | ✓ |
| Respect `X-Ozma-Shim-Transform-Budget` and `X-Ozma-Shim-Cost-Budget` ceilings | — | ✓ | ✓ | ✓ |
| Select client adapter from `ozmalib/client_adapters/*.yaml` based on Accept / request shape | ✓ | ✓ | ✓ | ✓ |
| Select model adapter from `ozmalib/model_adapters/*.yaml` based on upstream model id | ✓ | ✓ | ✓ | ✓ |
| Honor `X-Ozma-Mode: passthrough` — minimal transform; forward raw | ✓ | ✓ | ✓ | ✓ |
| Honor `X-Ozma-Mode: default` — full adaptation both sides | ✓ | ✓ | ✓ | ✓ |
| Honor `X-Ozma-Mode: force-protocol` — reject mismatched client/model pairs | ✓ | ✓ | ✓ | ✓ |
| Inject teaching prompts when pairing Ozma-client with stock-model (self-upgrade from v5.3) | — | ✓ | ✓ | ✓ |
| Parse stock-model output best-effort into Ozma structure when client is Ozma-native | — | ✓ | ✓ | ✓ |
| Emit `X-Ozma-Client-Tier`, `X-Ozma-Model-Tier`, `X-Ozma-Adapter-Applied` response headers | ✓ | ✓ | ✓ | ✓ |
| Inject `ROUTING-CONTEXT` at turn start per `X-Ozma-Routing-Context-Mode` | — | ✓ | ✓ | ✓ |
| Resolve `[routing:query ...]` sub-blocks and return matching subset | — | — | ✓ | ✓ |
| Refresh `ROUTING-CONTEXT` on TTL expiry and inject `[routing-context-delta]` | — | ✓ | ✓ | ✓ |
| Filter `available_models` by active compliance tags *before* injection | — | ✓ | ✓ | ✓ |
| Derive `enrichment_level` from `cost_vs_quality` when `auto` | — | — | ✓ | ✓ |
| Load WORK-UNIT by `X-Ozma-WorkUnit-Id` prefix (tl/mp/proj/adapter-scoped) | — | ✓ | ✓ | ✓ |
| Maintain task-list LRU and session-end eviction | — | ✓ | ✓ | ✓ |
| Maintain mini-project 90-day idle expiry; extend on re-touch | — | ✓ | ✓ | ✓ |
| Fetch project WORK-UNIT state via configured PM adapter | — | ✓ | ✓ | ✓ |
| Normalize open-vocabulary `categories` tags for consumer pattern matching | — | ✓ | ✓ | ✓ |
| Resolve PM-adapter id; fall back to `ozma-native` on unknown | ✓ | ✓ | ✓ | ✓ |
| Wrap MCP server via `mcp-passthrough` adapter | — | ✓ | ✓ | ✓ |
| Execute `on_complete` hooks on WORK-UNIT done status (respecting adapter `write_scope`) | — | ✓ | ✓ | ✓ |
| Perform `work-unit:promote` side-effects (memory write / PM ticket create) | — | ✓ | ✓ | ✓ |
| Redact PROJECT-CONTEXT per `project-context-redaction` rule category for training opt-in | — | ✓ | ✓ | ✓ |
| Propagate `dispatch_trace_id` across every descendant dispatch (W3C Trace Context) | — | — | ✓ | ✓ |
| Execute DISPATCH per strategy (cheapest-capable/cascade/race/vote/blend/ensemble/split/verify/human-gate/self) | — | — | ✓ | ✓ |
| Enforce three-axis budget (cost_usd / latency_ms / side_effects) per dispatch | — | — | ✓ | ✓ |
| Maintain `[rollup-ledger]` with per-dispatch breakdown and emit at turn end | — | — | ✓ | ✓ |
| Enforce `X-Ozma-Dispatch-Strategies` allowlist; reject disallowed strategies | ✓ | ✓ | ✓ | ✓ |
| Enforce `X-Ozma-Dispatch-Depth-Max` on `split` recursion | — | — | ✓ | ✓ |
| Apply rising-threshold, cool-down, batch-similar, explain-on-deny controls on `human-gate` | — | — | ✓ | ✓ |
| Enforce `approver_rules` (any_owner / all_owners / from_team:X / named:<id>) | — | ✓ | ✓ | ✓ |
| Propagate `[dispatch:cancel id=d:N]` to all descendants in the trace tree | — | — | ✓ | ✓ |
| Honor `X-Ozma-Dispatch-Stream` (interleave / buffer / separate-channel) | — | — | ✓ | ✓ |
| Emit `[from=d:N]…[/from]` provenance spans per `X-Ozma-Provenance` | — | — | ✓ | ✓ |
| Isolate sub-dispatch context to `context_required` refs (no parent-context leakage) | — | — | ✓ | ✓ |
| In `verify` strategy, spread targets across vendors per `targets_policy: diverse-capable` | — | — | ✓ | ✓ |
| Record strategy-quality telemetry (race_realized/vote_disagreement/blend_diversity/cascade_escalation/verify_agreement) | — | — | ✓ | ✓ |
| Record classifier-vs-model routing delta for A/B evaluation of the v5.8 inversion | — | — | ✓ | ✓ |
| Emit `X-Ozma-Dispatch-Trace-Id`, `X-Ozma-Rollup-*`, `X-Ozma-Dispatches-By-Strategy` response headers | — | — | ✓ | ✓ |
| Enforce `workspace_policy` (persistent / ephemeral / sandboxed) on dispatch side-effects | — | ✓ | ✓ | ✓ |
| Honor `X-Ozma-Dispatch: shim-execute \| client-execute \| forbid` routing-side switch | ✓ | ✓ | ✓ | ✓ |
| Resolve `X-Ozma-Agent-Id` to a signed Agent Card; auto-create on first-use if scope permits | — | ✓ | ✓ | ✓ |
| Verify A2A v1.0 Signed Agent Card signatures on inbound messages | ✓ | ✓ | ✓ | ✓ |
| Select coordination transport per deployment preference (`a2a \| grpc-a2a \| mcp-agent-mail \| amq-file`) | ✓ | ✓ | ✓ | ✓ |
| Negotiate `coordination_wire: standard \| ozma-compressed` per-peer on handshake | — | — | ✓ | ✓ |
| Content-address Agent Cards via Session Cache (`{@agent:sha256[:6]}`) | — | ✓ | ✓ | ✓ |
| Inject teaching-prompt on first peer message when `pre-taught` mode selected | — | — | ✓ | ✓ |
| Emit inline-legend (`// →send=method:message/send; ...`) on first message in `inline-hint` mode | — | — | ✓ | ✓ |
| Verify peer decode via two-way verification on first 2–3 messages; revert on confusion | — | — | ✓ | ✓ |
| Route messages to peer inbox via selected transport; enforce idempotency on delivery | — | ✓ | ✓ | ✓ |
| Dedupe messages by id across direct + channel delivery | — | ✓ | ✓ | ✓ |
| Apply `X-Ozma-Message-Rate-Limit` per-agent-pair window | — | ✓ | ✓ | ✓ |
| Filter outgoing messages by `compliance` tags against peer compliance set | — | ✓ | ✓ | ✓ |
| Inject `[inbox]` block at turn start with priority/TTL/subscription filtering | — | ✓ | ✓ | ✓ |
| Resolve `[message:fetch id=m:N]` requests from the inbox cache | — | ✓ | ✓ | ✓ |
| Maintain per-agent subscriptions; enforce `subscription_max` (default 32) FIFO eviction | — | ✓ | ✓ | ✓ |
| Resolve channel names (ozma-native and adapter-backed) | — | ✓ | ✓ | ✓ |
| Mirror channel messages to/from external adapters (Slack / Discord / Teams / IRC) per `write_scope` | — | ✓ | ✓ | ✓ |
| Maintain blocker-graph edges on WORK-UNIT; reject cycles | — | ✓ | ✓ | ✓ |
| On WORK-UNIT `status=done`, walk outgoing edges and auto-emit `unblock` to downstream owners | — | ✓ | ✓ | ✓ |
| Track claims (advisory); surface conflicts in ROUTING-CONTEXT for DISPATCH avoidance | — | ✓ | ✓ | ✓ |
| Auto-release expired claims | — | ✓ | ✓ | ✓ |
| Enforce `handoff_depth_max` per thread; escalate to `human-gate` on cap | — | — | ✓ | ✓ |
| Detect A→B→A→B oscillation and auto-escalate on second oscillation | — | — | ✓ | ✓ |
| Require `ack: accepted=true\|false` on `requires_ack: true` handoffs within `ack_timeout` | — | ✓ | ✓ | ✓ |
| Implement `message-triggered` wake only under explicit deployment authorization | — | — | ✓ | ✓ |
| Emit `X-Ozma-Messages-*`, `X-Ozma-Handoff-*`, `X-Ozma-Coordination-*`, `X-Ozma-Teaching-*` response headers | — | ✓ | ✓ | ✓ |
| Build and inject `[session-registry]` at turn start when `SESSION-REF` cap is active | — | — | ✓ | ✓ |
| Resolve `{@session:*}` refs via EMBED semantic retrieval (not whole-session dump) | — | — | ✓ | ✓ |
| Enforce session ACL before returning any cross-session content; return identical error for `unknown \| deleted \| share_denied \| sensitive_blocked` (prevents existence oracles) | ✓ | ✓ | ✓ | ✓ |
| Maintain per-user session index keyed by user × workspace × label | — | ✓ | ✓ | ✓ |
| Fire natural-language hint on trigger phrases (`the other session`, `yesterday's chat`, `like we discussed earlier`, …) | — | ✓ | ✓ | ✓ |
| Rank `[session:search]` candidates by semantic similarity + recency | — | — | ✓ | ✓ |
| Propagate `contains-secret` compliance tag to auto-flag sensitive sessions | ✓ | ✓ | ✓ | ✓ |
| Dedupe content-addressable chunks across sessions (same fingerprint stored once) | — | ✓ | ✓ | ✓ |
| Enforce `X-Ozma-Session-Scope` — refuse `global` if deployment forbids | ✓ | ✓ | ✓ | ✓ |
| Record session share grants in TRACE with `kind=session-share`; honor revocation | — | ✓ | ✓ | ✓ |
| Own TASK subprocess / tool-call lifecycle (spawn, pause/resume, signal, reap) | — | ✓ | ✓ | ✓ |
| Apply TASK elastic-await threshold (`task_elastic_threshold_ms`); return terminal Task synchronously below threshold, live handle above | — | ✓ | ✓ | ✓ |
| Maintain TASK stdout / stderr / events ring buffers (registry-sized); page to disk on overflow when configured | — | ✓ | ✓ | ✓ |
| Evaluate `wake_on` predicates against new output in O(new bytes); inject `[task-wake]` at turn boundary | — | ✓ | ✓ | ✓ |
| Inject `[task-status]` at turn boundaries per the TASK ambient cadence table (idle, 80%-budget, overrun, terminal) | — | ✓ | ✓ | ✓ |
| Enforce TASK `on_overrun` at budget elapsed exactly once (`suspend_and_ask` / `kill` / `warn` / `extend`) | — | ✓ | ✓ | ✓ |
| Parse JSONL events via `task_event_sentinel`; route sentinel lines to events channel, plain lines to stdout; emit `task_wake_on_malformed` on bad JSON | — | ✓ | ✓ | ✓ |
| Persist TASK metadata / stdout / stderr / events to configured `task-store` adapter per scope; atomic metadata rewrites | — | ✓ | ✓ | ✓ |
| Enumerate running workspace/user-scope tasks at session start; inject initial `[task-status]` injection per task | — | ✓ | ✓ | ✓ |
| Enforce TASK `auth` ACL on cross-session control ops; require `confirm=true` on peer destructive ops (`cancel`, `kill`) | — | ✓ | ✓ | ✓ |
| Cascade-cancel child tasks (by `parent_id`) when parent is cancelled/killed | — | ✓ | ✓ | ✓ |
| On shim restart, re-attach to live TASK subprocesses by PID; mark orphans `status=failed exit_code=-1 reason="shim_restart"` | — | ✓ | ✓ | ✓ |
| Retain terminal TASK handles per `task_retention_per_{session,workspace,user}`; archive beyond retention; prune on `task_archive_ttl_days` | — | ✓ | ✓ | ✓ |
| Emit `task.*` events on the COORDINATION event surface; honor `task.*` subscriptions and SCHEDULE `on-event` triggers | — | ✓ | ✓ | ✓ |
| Expose prose-fallback tool pair (`spawn` / `task(id, op, ...)`) when model lacks TASK capability; full lifecycle runs server-side | — | ✓ | ✓ | ✓ |

---

## Shim Profile and Transform Bridges

The shim is a first-class protocol participant, not an implicit
passthrough. It declares its capabilities in a shim profile;
those declared capabilities participate directly in handshake
negotiation. This lets a simple client obtain rich multimodal
behavior without implementing any new renderers itself, and it
makes the deployment's value-add explicit and auditable.

### Shim profile

A single YAML file — `ozmalib/shim_profile.yaml` — declares:

1. **Renderers** available server-side (html-to-image, svg-to-
   raster, markdown-to-html, vrm-to-video, mermaid-to-png,
   latex-to-png, plotly-to-png).
2. **Transcoders** (image: AVIF ↔ WebP ↔ JPEG ↔ PNG; video:
   AV1 ↔ H.265 ↔ H.264 ↔ VP9; audio: Opus ↔ MP3 ↔ WAV ↔ FLAC).
3. **TTS adapters** (SSML → audio/mp3) bound to provider backends
   (ElevenLabs / Azure Speech / OpenAI TTS / Google Cloud TTS /
   Amazon Polly / Piper local).
4. **Vision pipelines** (YOLO / SAM / MediaPipe / OCR / VLM)
   available for server-side perception.
5. **Motion transports** (ROS 2 / OPC UA / DDS / VMC-OSC /
   Autoware / MAVLink / MQTT / REST / vendor SDKs) available
   for MOTION dispatch.
6. **Composed bridges** — chained transforms (e.g., `rich-html →
   png` via the html-to-image renderer; `vrm + motion → h264`
   via a VRM animator plus video encoder).
7. **Cost and latency envelopes** per transform (for budget
   enforcement).

**Example:**

```yaml
# ozmalib/shim_profile.yaml
shim_id: shim-prod-us-east-1
version: 1.3.0

renderers:
  - id: html-to-image
    tool: playwright-chromium
    output: image/png
    max_viewport: [1920, 1080]
    cost_usd_per_call: 0.0004
    p95_latency_ms: 650
  - id: svg-to-raster
    tool: resvg
    output: image/avif
    cost_usd_per_call: 0.00001
    p95_latency_ms: 18
  - id: markdown-to-html
    tool: markdown-it
    output: text/html
    cost_usd_per_call: 0.000001
    p95_latency_ms: 3
  - id: vrm-to-video
    tool: ozma-vrm-animator
    output: video/mp4
    codec: h265
    cost_usd_per_call: 0.008
    p95_latency_ms: 1800
  - id: mermaid-to-png
    tool: mermaid-cli
    output: image/png
    cost_usd_per_call: 0.0003
    p95_latency_ms: 420
  - id: latex-to-png
    tool: tectonic + pdf-render
    output: image/png
    cost_usd_per_call: 0.0006
    p95_latency_ms: 900

transcoders:
  image:
    - { from: avif, to: webp, cost: 0.000002, p95_ms: 12 }
    - { from: avif, to: jpeg, cost: 0.000002, p95_ms: 10 }
    - { from: webp, to: avif, cost: 0.000002, p95_ms: 14 }
    - { from: raw-rgb, to: avif, cost: 0.000003, p95_ms: 22 }
  video:
    - { from: av1, to: h265, cost: 0.001, p95_ms: 300 }
    - { from: h265, to: h264, cost: 0.0008, p95_ms: 250 }
  audio:
    - { from: opus, to: mp3, cost: 0.000005, p95_ms: 20 }
    - { from: wav, to: opus, cost: 0.000005, p95_ms: 18 }

tts_adapters:
  - { id: elevenlabs-v3, ssml: partial, output: audio/mp3, cost_per_1k_chars: 0.18 }
  - { id: azure-neural, ssml: full,    output: audio/mp3, cost_per_1k_chars: 0.016 }
  - { id: openai-tts,   ssml: none,    output: audio/mp3, cost_per_1k_chars: 0.015 }
  - { id: piper-local,  ssml: partial, output: audio/wav, cost_per_1k_chars: 0.0 }

vision_pipelines:
  - { id: yolov11x,       input: image, output: detections }
  - { id: sam2,           input: image, output: masks }
  - { id: mediapipe-holistic, input: image, output: keypoints }
  - { id: tesseract-ocr,  input: image, output: text-boxes }
  - { id: paddleocr,      input: image, output: text-boxes }
  - { id: llava-next,     input: image, output: caption }

motion_transports:
  - ros2
  - opcua
  - dds
  - vmc-osc
  - autoware
  - mavlink
  - mqtt
  - rest
  - vendor:spot-sdk
  - vendor:universal-robots

composed_bridges:
  - id: rich-html-to-image
    chain: [markdown-to-html, html-to-image]
    produces: image/png
  - id: vrm-motion-to-video
    chain: [vrm-animator, h265-encode]
    produces: video/mp4
  - id: ssml-prosody-to-audio
    chain: [prosody-normalize, azure-neural]
    produces: audio/mp3
```

### Effective capability computation

Given client-declared capabilities `C` and shim-declared
transforms `T`, the effective capability set is:

```
effective = C ∪ { x | (x → y) ∈ T  ∧  y ∈ C }
         ∪ { x | chain(x → ... → y) ∈ T_composed  ∧  y ∈ C }
```

That is, the model may emit any capability `x` that either the
client can render directly, or that the shim can transform into
something the client can render. The effective set is advertised
in the handshake system-message alongside the raw client caps,
and echoed in the response header `X-Ozma-Effective-Caps`.

**Worked example.** A phone client declares
`Accept-Media: image/jpeg, text/plain, audio/mp3`. The shim
declares the full profile above. Then:

| Capability | Direct | Via transform | Effective? |
|---|---|---|---|
| `plain` | ✓ | — | ✓ |
| `image/jpeg` | ✓ | — | ✓ |
| `image/avif` | — | `avif → jpeg` | ✓ |
| `image/webp` | — | `webp → jpeg` | ✓ |
| `rich-html` | — | `markdown-to-html + html-to-image → png → jpeg` | ✓ |
| `vision-annotate` | — | SVG overlay → burn-in via `svg-to-raster → jpeg` | ✓ |
| `ssml` | — | `ssml → audio/mp3` via Azure | ✓ |
| `audio/mp3` | ✓ | — | ✓ |
| `vrm` (live) | — | `vrm → h264` (no video codec on client) | ✗ |
| `audio/wav` | — | no transform to mp3 | — (implicit via opus→mp3 path if wav is intermediate) |

The phone — advertised as a text/jpeg/mp3 client — effectively
receives the full multimodal experience *except* live VRM.

### Transform budgets

Transforms cost CPU time and money. Two budget headers cap
their use per turn:

| Header | Semantics |
|---|---|
| `X-Ozma-Shim-Transform-Budget` | Max number of transforms per turn (integer; default 8) |
| `X-Ozma-Shim-Cost-Budget` | Max transform cost per turn in USD (float; default 0.05) |

When either budget is exceeded, the shim falls back to the next
cheapest rendering (e.g., `rich-html → png → jpeg` fails the
budget → drop to plaintext rendering). The fallback decision is
logged to telemetry.

### Response headers

```
X-Ozma-Shim-Transforms-Applied: markdown-to-html,html-to-image,png-to-jpeg
X-Ozma-Shim-Transform-Latency-Ms: 712
X-Ozma-Shim-Transform-Cost-Usd: 0.00041
X-Ozma-Effective-Caps: plain,image/jpeg,image/avif,rich-html,vision-annotate,ssml,audio/mp3
```

### Multi-tenant deployment

Different deployments can ship different shim profiles without
model retraining. A cost-sensitive deployment omits html-to-
image; a premium deployment includes vrm-to-video and Azure
Neural TTS. The model is agnostic — it emits the maximal useful
capability set; the shim decides what the local environment can
actually render.

---

## Adapter — unified registry primitive

The spec uses one consistent `Adapter` primitive across all its
integration surfaces (client transports, upstream models, PM
systems, coordination transports, channels, sensors, devices, TTS,
voices, themes, styles, personas, memory backends, emit targets,
gates). One YAML shape with a `kind:` discriminator replaces ~16
parallel per-adapter-kind specs.

### Canonical shape

```yaml
# ozmalib/adapters/<kind>/<id>.yaml
id: linear
kind: pm                                # discriminator — see kind table
version: 1.2.0
auth:
  mode: oauth2                          # oauth2 | api-key | hmac | mtls | bearer | env
  env: LINEAR_API_TOKEN                 # or inline credentials ref
  scope: [read, write]
backend:
  kind: graphql                         # transport to reach this adapter's upstream
  endpoint: https://api.linear.app/graphql
  auth_header: "Authorization: Bearer {{env:LINEAR_API_TOKEN}}"
scope:
  read: [projects, tickets, sprints, velocity, decisions]
  write: [ticket_comment, ticket_close, ticket_create, ticket_update]
detect:                                  # optional — for auto-detection where applicable
  path: ["/v1/chat/completions"]        # e.g. client-adapter detection
  header: {}
capabilities: [...]                      # kind-specific capability declaration
mapping: {...}                           # kind-specific field mapping (request/response)
cost:
  cost_usd_per_call: 0.00001
  p95_latency_ms: 180
compliance: [soc2, us-only]
```

Required fields: `id`, `kind`, `auth`, `backend`. Everything else
is optional or kind-specific. The outer envelope is identical for
every adapter kind.

### Kinds (initial registry)

| Kind | Role | Scope |
|---|---|---|
| `client` | Adapt incoming client protocol to Ozma envelope (OpenAI Chat Completions, Anthropic Messages, Gemini, LiveKit, Daily, WebSocket-chat, curl-basic, ozma-native) | Ingress |
| `model` | Adapt Ozma calls to upstream model API (openai-chat-tools, anthropic-messages-tools, google-generate-content, completion-api, local-llama-cpp, ollama, vllm-openai, hf-inference) | Egress-up |
| `pm` | Project-management integration (linear, jira, github-issues, asana, notion, clickup, ozma-native, mcp-passthrough) | PM writes / reads |
| `agent-transport` | Peer-to-peer coordination transport (ozma-native, a2a, a2a-grpc, mcp-agent-mail, amq-file) | Peer coordination |
| `channel` | Broadcast channel backing (ozma-native, slack, discord, teams, irc, mcp-passthrough) | Peer coordination |
| `sensor` | Perception pipeline (vision: yolo/sam/mediapipe/ocr/vlm; listen: whisper/vad/diarize; sensor: imu/gps/depth/thermal/bio) | Perception |
| `device` | Controllable embodied system (avatar / robot-arm / vehicle / drone / iot) | Embodiment |
| `motion-transport` | Motion dispatch wire (ros2, opcua, dds, vmc-osc, autoware, mavlink, mqtt, rest, vendor-sdk) | Embodiment |
| `tts` | Speech synthesis backend (elevenlabs-v3, azure-neural, openai-tts, google-tts, piper-local) | Audio output |
| `voice` | Persona voice identity resolution (per engine; voice id mapping) | Audio output |
| `theme` | Visual presentation resolution (emoji density, color palette, formatting rules per render target) | Rich output |
| `style` | Communication-style definition (conversational, socratic, terse, technical-spec, caveman, pirate, ...) | Text output |
| `persona` | Named persona profile with traits, memory scope, voice / avatar bindings | Identity |
| `memory` | Memory store backend (in-process, postgres, redis, chromadb, pinecone, ...) | Persistence |
| `emit` | External-action target (slack-webhook, email-sendgrid, twilio-sms, http-webhook, stripe-charge, github-pr, mcp-action-passthrough, ...) | Egress (v5.10) |
| `gate` | Gating backend (opa, cedar, casbin, policy-as-prompt, ozma-human-gate, external-api, quorum, payment) | Pre-action check (v5.10) |
| `triggers` | Scheduled-wake backend (ozma-native-scheduler, claude-code-remote-triggers, anthropic-managed-agents, temporal, aws-eventbridge, github-actions-schedule, custom-http) | SCHEDULE firing (v5.11) |
| `task-store` | Long-running task backend (filesystem, redis, postgres, sqlite, memory) — stores metadata/stdout/stderr/events per scope; owns cross-session publication layout (`~/.ozma/tasks/<scope>/<owner>/<task_id>.*`) | TASK persistence (v5.15) |

The kind set is extensible — drop a new YAML file with a new
`kind:` value plus a matching loader module. No spec change
required to add a kind.

### Loader behavior

1. Shim startup scans `ozmalib/adapters/*/**.yaml`.
2. Each file's `kind` selects a validator (one per kind; validates
   the kind-specific fields beyond the common envelope).
3. Adapters register with the shim's registry keyed by
   `(kind, id)`.
4. Turn-time resolution uses `X-Ozma-<Kind>-Adapter: <id>` headers
   plus detection rules for auto-selection (where applicable, e.g.,
   client-adapter detection from request shape).

### Write scope and auth

Every adapter declares `auth.scope` and `backend.scope` explicitly.
Adapters default to read-only (`write: []`); writes are opt-in per
deployment configuration, consistent with §Protocol Compliance
Adaptation and v5.8 PM-adapter write-scope semantics.

### Why unification

Before v5.9 the spec specified ~16 adapter kinds as parallel
concepts, each with its own loader, YAML shape, header format,
and error kinds. Consolidation:

- Removes ~600 lines of parallel per-kind spec.
- One loader, one validator dispatcher, one header namespace
  (`X-Ozma-<Kind>-Adapter`, `X-Ozma-<Kind>-Adapter-Option`).
- One error kind family (`adapter_unknown` with `kind:` and `id:`
  fields) replacing per-kind error kinds.
- One telemetry namespace (`adapters_loaded_by_kind`,
  `adapter_<kind>_<metric>`).
- New adapter kinds add themselves without touching the core spec.

Legacy references to e.g. "PM adapter", "TTS adapter", "sensor
profile" remain in prose for readability; they all resolve to the
unified Adapter primitive under the hood.

### Rate limits on Adapters (v5.9.1)

Any Adapter may declare rate limits using the unified §RATE_LIMIT
grammar. Rate limits compose across scopes (per-user limit AND
per-deployment limit both apply; the most restrictive wins):

```yaml
rate_limits:
  - { scope: per-user,       window: "+1h",  max: 1000,     overage: warn }
  - { scope: per-session,    window: "+5m",  max: 60,       overage: queue }
  - { scope: per-agent-pair, window: "+5m",  max: 60,       overage: queue }   # COORDINATION default
  - { scope: per-deployment, window: "+1d",  max: 100000,   overage: reject }
  - { scope: per-adapter,    window: "+1s",  max: 10,       overage: queue }   # raw throughput
  - { scope: per-workunit,   window: "+1h",  max: 500,      overage: warn }
  - { scope: per-cap,        window: "+1m",  max: 20,       overage: queue }   # e.g. DISPATCH
```

Overage policies:
- `queue` — excess requests held until the window clears
- `reject` — excess requests fail with `rate_limit_exceeded`
- `warn` — requests pass but trace entries carry `rate_limit_warn`

Rate-limit evaluation produces trace entries (`kind=rate-limit-check`)
and participates in the unified audit. Consolidates:
- v5.9 per-agent-pair message rate limits (now `scope=per-agent-pair`)
- v5.8 DISPATCH three-axis budgets (now expressed as per-turn
  `scope=per-session max=<cost_usd>` overage=`reject`)
- v5.8 monthly budget caps (now `scope=per-user window=+30d`)
- Adapter-level raw throughput limits (now `scope=per-adapter`)

---

## Gate — unified pre-action check

Every "should this action proceed?" check in the protocol uses one
primitive. Subsumes:

- v5.5 MOTION `safety.requires_confirm` (human confirmation before
  dispatching to a device)
- v5.8 DISPATCH `human-gate` strategy (approval before an
  expensive/destructive dispatch)
- v5.8 CONFIDENCE review queues (low-confidence edits waiting
  human review)
- v5.8 compliance pre-injection filter (before ROUTING-CONTEXT
  shows a non-compliant model)
- v5.9 COORDINATION claim conflicts (advisory, surfaced to
  orchestrator)
- v5.10 proposed external-action gating (OPA policy, risk-engine,
  human-approve, quorum, informational-only audit)

All share: *proposed action → predicate evaluated → decision from
a fixed set*. The conceptual distinction between "gate" and
"review" dissolves — they're the same shape.

### Canonical shape

```
[gate id=g:<n> on=<action-ref> kind=<kind> priority=<0.0-1.0>]
  ...kind-specific fields...
  decision_policy: blocking | informational | composite
  on_decision:
    approve:  continue
    deny:     { behavior: refuse, surface_to: initiator, reason_required: true }
    modify:   { behavior: substitute, review_modified: optional }
    delay:    { behavior: queue, resume_when: <predicate> }
    escalate: { behavior: forward, to: <higher-gate-id> }
[/gate]
```

### Kinds (eleven, extensible)

| Kind | Backing | Use |
|---|---|---|
| `human` | Human approver via client UI; `approver_rules: any_owner \| all_owners \| from_team:X \| named:<id>`; `cool_down_seconds`, `rising_threshold`, `batch_similar`, `explain_on_deny` controls (v5.8 fatigue mitigation) | Destructive / high-blast-radius / P0 |
| `policy` | Declarative rule engine via `gate`-adapter (OPA Rego, AWS Cedar, Casbin, or `policy-as-prompt` prose policies evaluated by a fast Token Model) | Compliance, access control |
| `rules` | Inline deterministic predicate (JSON-logic, no external engine) | Simple runtime checks |
| `algorithm` | HTTP endpoint returns a score; `threshold_max` / `threshold_min` compares | Risk scoring, fraud detection |
| `quorum` | N-of-M peer agents must agree (COORDINATION `request purpose=review` fan-out; `verdict` counts) | Collective-judgment decisions |
| `external-api` | Async webhook to an external review system; `timeout`; `escalate_on_timeout` | SOX / HIPAA / enterprise review queues |
| `informational` | Always passes; logs + forwards to audit backends | Regulatory audit-trail, training signal |
| `payment` | A2A AP2 / Stripe / escrow integration for value transfers | Agent-initiated transactions |
| `kill-switch-check` | Fast-path: is there an active kill-switch in scope? | Safety floor under every action |
| `composite` | Chain of gates with semantics `required \| any \| all-informational` | Multi-layer review stacks |
| `authority` (v5.13) | Cryptographically-signed, scope-bound, revocable external attestation from a designated issuer (operator / contract / regulator / comms channel). Live revocation pull (or cached with TTL); offline-unsafe by default. | Dual-use and high-consequence actuation — autonomous weapons / defence, aircraft fire-class actuation, medical delivery above threshold, financial wire transfer above threshold, regulated-utility actuation. External authority is the legal / contractual basis for the action; model proposes, authority authorises, shim gates. |

### `authority` gate kind (v5.13 — normative)

Gate kind=`authority` inverts the alignment problem for dual-use
actuation: the substrate does not move unless an external, authoritative
issuer currently attests that this specific action is permitted. The
model proposes; an external issuer authorises; the shim gates. Structural
parallel to PCI tier-0 — the merchant never holds the PAN, and the
model never holds the fire-authority. Both are opaque handles granted
by an external trust root, revocable at source.

**Required fields (normative):**

| Field | Type | Semantics |
|---|---|---|
| `issuer` | DID \| URL \| public-key fingerprint | Who is authorised to emit the attestation. Registry-pinned; a deployment's issuer set is configuration, not model-influenceable. |
| `attestation_type` | `contract_binding \| operator_approval \| regulatory_license \| comms_handshake` | What class of authorisation satisfies the gate. Determines the verification path. |
| `attestation` | signed blob (JWS / COSE / DSSE / equivalent) | The live token; verified against the issuer's key material at gate-evaluation time. |
| `valid_for` | ISO-8601 window \| `converge:<id>` \| `action-count:N` | When / how long the attestation authorises action. A converge-bound attestation expires when its converge terminates. |
| `revocation_check` | `live-pull \| cached-with-TTL \| offline-unsafe` | Default: `live-pull`. Offline operation requires explicit opt-in at registry time; `cached-with-TTL` requires a TTL ≤ the deployment's compliance-tier maximum. |
| `scope` | `one-action \| action-class:<class> \| device-class:<class>` | What the attestation authorises. Registry rejects gates with broader scope than the deployment's safety envelope permits. |
| `issuer_epoch` | integer | Monotonic counter advanced by the issuer on key rotation, disclosure, or compromise response. Shim rejects attestations whose embedded epoch is below the issuer's currently-advertised minimum epoch — pinned per registry, refreshed on the same cadence as `revocation_check`. Closes the "stolen key indefinitely valid" window. |
| `authority_chain` | ordered list of `(issuer, attestation, epoch)` tuples | Optional. Multi-step delegation chain (e.g., regulator → state → operator → device). Each link's issuer must be authorised to delegate to the next link's issuer under the registry's issuer-relationship graph. Shim verifies every link independently; chain fails closed on any missing, expired, revoked, or unauthorised link. |

**Chain of authority.** Real-world attestation is rarely single-issuer.
A civilian drone's fire-class actuator may require: a regulatory
licence (aviation authority) → a state authorisation (defence
ministry) → an operator sortie-binding (mission operator) → a
last-mile comms handshake (forward controller). `authority_chain`
encodes this as an ordered tuple list; the shim verifies every link
against the registry-pinned issuer-relationship graph and fails
closed if any link is unauthorised to delegate to its successor.
Delegation scopes *narrow* down the chain (a regulator can bind a
state to civilian-only scope; the state cannot broaden back out) —
the registry enforces monotone narrowing.

**Issuer-key rotation and compromise.** `issuer_epoch` is a forward-
only counter advanced by the issuer on routine key rotation, on
suspected compromise, or on any operational event that should
invalidate outstanding attestations. The shim pins the issuer's
currently-advertised minimum epoch and refreshes on the
`revocation_check` cadence; any attestation whose embedded epoch is
below the pinned minimum evaluates to `deny` regardless of signature
validity. This closes the class of attacks where a leaked signing
key remains usable until every outstanding attestation expires on
its `valid_for` clock — with `issuer_epoch`, one epoch bump at the
issuer invalidates every outstanding credential at next
revocation-check.

**Decision semantics.** Unlike other gate kinds, `authority` gates are
**default-closed**: absence of a valid attestation evaluates to `deny`,
not `delay`. An unreachable issuer under `live-pull` evaluates to
`deny` (the offline-unsafe posture). Operators wanting a different
posture must explicitly configure `cached-with-TTL` with a bounded
TTL; the registry tracks the choice for audit.

**Revocation semantics.** Revocation is a one-sided network event. A
running converge that depended on a now-revoked attestation must halt
on its next revocation check; the revocation-check interval is part
of the gate's configuration and bounded by the device profile's
safety envelope (see §MOTION → Safety contract). Reference
implementation checks every `revocation_check_interval_ms` during
a live motion, and additionally on every new motion verb.

**Integration with MOTION.** Device profiles may declare
`requires_authority: true` and a required `attestation_type`. The shim
refuses any motion verb on such a device unless a `authority` gate
resolves to `approve`. The gate composes naturally with existing
MOTION safety machinery (`requires_confirm`, kill-switch, geofence).
Typical composite for a fire-class actuator:

```
[gate id=g:fire-composite on=motion:fire kind=composite]
  semantics: required
  chain:
    - g:kill-switch                   # kind=kill-switch-check
    - g:authority-contract            # kind=authority; attestation_type=contract_binding
    - g:geofence-policy               # kind=policy engine=opa
    - g:human-confirm-lethal          # kind=human approver_rules=from_team:weapons-officer
    - g:audit-ihl                     # kind=informational log_to=ihl-audit
[/gate]
```

**Integration with IFC (§3.10).** An `authority` attestation is itself
a tier-0 opaque handle: the shim stores the attestation blob, and the
model never receives the signed bytes — only the gate *decision*
(`approve` / `deny`) and an opaque `attestation_id`. This prevents the
model from forging, paraphrasing, or reasoning about the credential.

**Integration with converge (§3.9).** A converge that spans multiple
motion verbs binds its authority gate with `valid_for: converge:<id>`;
the attestation is scoped to that converge's lifetime. Revocation mid-
converge halts motion on the next revocation check and terminates the
converge with `authority_revoked` as the termination reason —
one of the nine normative termination reasons defined in §3.9
(`setpoint_reached`, `termination_predicate_true`, `bound_exceeded`,
`timeout`, `kill_switch`, `human_halt`, `authority_revoked`,
`observation_unavailable`, `external_halt`).

**Audit.** Every authority-gate evaluation produces a TRACE entry with:
`gate_id`, `attestation_id`, `issuer_fingerprint`, `attestation_type`,
`signed_at`, `valid_for`, `scope`, `revocation_checked_at`,
`revocation_status`, `decision`, `reason`. Audit trails for
`authority`-kind evaluations are retained according to the deployment's
compliance tier (minimum seven years for regulated-industry defaults,
overridable upward).

**Dual-use posture (see §Extension Points and §Limitations in
companion thesis).** Gate kind=`authority` is policy-neutral
machinery. It structurally enforces whichever authority regime the
deployment is bound to; it does not adjudicate which regimes are
legitimate. Analogous to TLS as policy-neutral cryptographic
machinery on which export-control policy rides.

**Research grounding.** The signing / attestation / revocation
primitives are standard: JWS (RFC 7515), CBOR Object Signing and
Encryption (COSE — RFC 9052 / RFC 9053), DSSE (in-toto Dead Simple
Signing Envelope) for build-provenance-shaped attestations, OCSP
(RFC 6960) and OCSP stapling for live revocation, and Sigstore's
transparency-log pattern for public auditability. Chain-of-authority
follows X.509 delegation and SPIFFE / SPIRE workload-identity
federation. The human-authority / IHL framing is grounded in DoD
Directive 3000.09 (autonomy in weapon systems — human judgment
over use of force), the ICRC *IHL and autonomous weapons* position,
NATO's 2021 *Principles of Responsible Use of AI in Defence*, and
the DARPA Assured Autonomy programme. Gate kind=`authority` is the
structural bridge between these two literatures: signed, scoped,
revocable cryptographic attestation as the mechanical enforcement
surface for externally-adjudicated authorisation regimes.

### Decision set (universal, five)

| Decision | Semantics |
|---|---|
| `approve` | Action proceeds unmodified |
| `deny` | Action refused; reason returned; caller may retry with changes |
| `modify` | Gate returns adjusted action (`{field: new_value, ...}`); caller accepts or rejects modification |
| `delay` | Action queued; resumed when `resume_when` predicate true (market open, business hours, human-available, ...) |
| `escalate` | Action forwarded to a higher-authority gate id |

### Composition

`composite` gates chain simpler gates:

```
[gate id=g:trade-composite on=emit:e:42 kind=composite]
  semantics: required                   # required | any | all-informational
  chain:
    - g:policy-opa                       # kind=policy engine=opa
    - g:risk-algo                        # kind=algorithm endpoint=...
    - g:market-hours                     # kind=rules predicate=market_is_open
    - g:sox-audit                        # kind=informational log_to=sox-audit
    - g:human-gate-over-10k              # kind=human only_if="trade_size_usd > 10000"
[/gate]
```

Semantics:
- `required` — all gates must `approve` (or `modify` accepted);
  first `deny` / `escalate` short-circuits.
- `any` — first `approve` passes; deny/escalate only if all deny.
- `all-informational` — all pass; any blocking decisions from
  child gates are downgraded to log-only (regulated audit
  without latency).

### Integration points (where GATEs are installed)

| Surface | Default gate |
|---|---|
| DISPATCH with strategy=`human-gate` | kind=human with fatigue controls |
| DISPATCH with destructive-op detection | kind=composite: kill-switch + human |
| EMIT (v5.10 external action) | kind=composite: policy + rules + informational + optional human |
| MOTION commands in `safety.requires_confirm` | kind=human |
| CONFIDENCE review queue | kind=human with `rising_threshold` |
| ROUTING-CONTEXT model filter | kind=rules (compliance pre-injection) |
| Claims conflict | kind=informational (advisory only, surfaced not blocked) |

### Audit

Every gate evaluation produces a TRACE entry (see §Trace and Audit)
with: `gate_id`, `kind`, `decision`, `reason`, `modifications?`,
`approver?`, `latency_ms`. Approvals, denials, modifications, and
delays are all visible to operators and retrievable for compliance
reporting.

### Fatigue controls (universal, not just human)

The v5.8 human-gate fatigue controls (rising_threshold, cool_down,
batch_similar, explain_on_deny) generalize to any gate kind where
repeated similar decisions occur:

- `rising_threshold` — after N incident-free approvals of
  structurally identical gates, the gate auto-passes the (N+1)th
  (with telemetry flag `auto_approved_by_prior`). Resets on any
  denial. Applies to human, algorithmic, policy gates alike.
- `cool_down_seconds` — mandatory wait before a decision can be
  rendered (human re-read time; algorithmic re-eval with fresh
  data).
- `batch_similar` — structurally similar pending gates grouped
  into one review surface.
- `explain_on_deny` — denial requires reason; feeds back to future
  similar decisions as negative example.

---

## Converge — bounded adaptive control loops (§3.9, v5.13 — normative)

`converge` formalises the pattern that a *bounded adaptive control
loop* — setpoint, observation, adjustment, termination — is a
structurally recurring shape across otherwise-unrelated primitives:
MOTION trajectories tracking a pose, SENSOR controllers holding a
temperature, DISPATCH retry loops seeking a verified answer, planner
refinement converging on an accepted plan, authority-gated actuation
held open for the duration of an attestation, and every other place
the protocol has quietly re-implemented the same shape. One
declarative form replaces those per-subsystem ad-hoc reimplementations
and ensures each one carries the same safety, audit, and composition
guarantees.

### Shape

```
[converge id=c:<n> kind=<kind> on=<subject>]
  setpoint:     <expression | ref>           # the target to reach
  observation:  <verb | sense-ref>           # how progress is measured each tick
  adjustment:   <action | plan-ref>          # what is applied between ticks
  termination:
    predicate:           <expression>        # optional — early-exit condition
    setpoint_tolerance:  <number>            # optional — distance at which setpoint is "reached"
    stable_for_ms:       <ms>                # optional — setpoint must hold for this long before exit
  bounds:
    max_iterations:        <int>             # mandatory — hard loop cap
    max_wall_ms:           <ms>              # mandatory — hard clock cap
    max_adjustment_effort: <scalar>          # optional — kind-specific (e.g. total motion energy, tokens, dollars)
    authority:             <gate-ref>        # optional — attestation whose revocation halts the loop
  tick_ms:      <ms>                         # sample / adjustment cadence
  on_termination:
    <reason>: <action-ref | handoff-ref>     # optional per-reason action (e.g. `bound_exceeded: notify`)
[/converge]
```

### Kinds

| Kind | Typical subject | Observation source | Adjustment form |
|---|---|---|---|
| `motion` | Physical actuator | SENSOR / kinematic state | MOTION verb (trajectory segment) |
| `sensor-control` | Environmental setpoint (temperature, flow, altitude) | SENSOR stream | Transport command |
| `dispatch-retry` | External call / verification | Tool result / verifier pass | DISPATCH with updated context |
| `plan-refinement` | PLAN acceptability | Adjudicator / quorum / human | Revised PLAN block |
| `agent-goal` | AGENT-STATE progress | Goal-progress predicate | Next sub-task selection |
| `composite` | Nested converges | Child termination events | Child reconfiguration |

`kind` is advisory for dispatch of defaults (tick cadence, default
bounds, which subsystem supplies `observation` / `adjustment`) but is
not a permissions axis; all kinds share the same termination vocabulary
and audit surface.

### Termination reasons (universal)

Every converge terminates with exactly one reason. The vocabulary is
fixed:

| Reason | Meaning |
|---|---|
| `setpoint_reached` | `setpoint_tolerance` satisfied and `stable_for_ms` held. |
| `termination_predicate_true` | `termination.predicate` evaluated true. |
| `bound_exceeded` | Any `bounds.*` cap exceeded (iteration / wall time / effort). |
| `timeout` | `max_wall_ms` elapsed without convergence. (Distinct from `bound_exceeded` for auditing intent: timeout is operator-observable time; bound_exceeded covers effort-class caps.) |
| `kill_switch` | Kill-switch gate fired during loop. |
| `human_halt` | A `kind=human` gate at the subject denied mid-loop. |
| `authority_revoked` | The converge's bound authority attestation was revoked or its issuer epoch advanced. |
| `observation_unavailable` | Observation source unreachable beyond its transport SLA. Fails closed: motion halts, setpoint holds last known safe pose. |
| `external_halt` | Client, upstream agent, or monitor emitted a halt signal. |

Termination reasons are normative. Additional reasons require a minor-
version bump and registry declaration.

### Safety posture (fail-closed by default)

Converge is **fail-closed**. Every termination reason other than
`setpoint_reached` and `termination_predicate_true` evaluates to the
subject's `emergency_stop_action` (for MOTION), to `best-effort-halt`
(for DISPATCH / network-control), or to "do not emit" (for
plan-refinement / agent-goal). Deployments wanting different behaviour
must configure per-reason `on_termination` handlers; the registry
records the override for audit.

If `observation_unavailable` fires and no `on_termination` handler is
declared, the shim treats it as `bound_exceeded` for audit purposes
and triggers the same fail-closed action. Silent hang is not a
valid outcome.

### Integration with Gate

Gates may be installed at three points on a converge:
- **pre-entry** — evaluated once; denial blocks the loop from starting.
- **per-tick** — evaluated every tick_ms against the proposed
  adjustment. `kill-switch-check` and `authority` gates default to
  per-tick. Denial at a tick terminates with the gate's kind mapped
  to a termination reason (`kill-switch` → `kill_switch`,
  `authority` → `authority_revoked`, others → `bound_exceeded`
  with `gate_id` in audit).
- **on-termination** — evaluated at termination; typically
  `informational` for audit.

The `bounds.authority` shortcut binds a specific authority attestation
to the converge's lifetime and installs it as a per-tick gate
automatically.

### Integration with MOTION

MOTION's in-flight safety behaviour (kill-switch poll, geofence
check, trajectory re-plan) is the `kind=motion` converge expressed
concretely. A single named motion (`goto x y z`) is sugar for a
converge with `setpoint = pose(x,y,z)`, `observation = kinematic
state`, `adjustment = trajectory segment`, termination
`setpoint_reached`, and device-profile bounds. The protocol
previously described this behaviour informally across §MOTION and
§Gate; §3.9 makes it a declared loop.

### Integration with IFC (§3.10)

Every tick's `observation` and `adjustment` carry IFC labels; the
converge's emitted telemetry inherits the label-join of all observed
artefacts across the loop's lifetime. A converge observing §PII
data emits §PII-labelled telemetry even if individual adjustment
actions are §PUBLIC. See §3.10 for the label-join rule.

### Audit

Every converge emits a TRACE record with `converge_id`, `kind`,
`subject`, `setpoint`, `bounds`, `tick_count`, `wall_ms`,
`termination_reason`, and per-tick sub-records at a sampling rate
declared in the deployment profile (default every tick for
`authority`-bound converges; every 10th tick otherwise).

### Registry fields

| Field | Default | Notes |
|---|---|---|
| `converge_default_tick_ms` | `100` | Shim default tick when omitted by the model. Motion overrides per device profile. |
| `converge_max_iterations_cap` | `10000` | Upper bound on `bounds.max_iterations` regardless of what the model declares. |
| `converge_max_wall_ms_cap` | `3600000` (1 h) | Upper bound on `bounds.max_wall_ms`. |
| `converge_observation_sla_ms` | `2000` | After this long without observation, `observation_unavailable` fires. |
| `converge_per_tick_trace_sample` | `authority: 1.0, default: 0.1` | Per-kind trace sampling rate. |

---

## Information-Flow Classification — IFC lattice (§3.10, v5.13 — normative)

IFC gives every artefact passing through the shim a *classification
label* drawn from a small declared lattice, and propagates the label
through every derivation. Gates may deny on label; egress is blocked
when an artefact's label exceeds the destination's permitted tier;
`§SECRET`-class data never enters the model's context at all — only
opaque handles. IFC is the structural-compliance half of the protocol
(Gate is the procedural half): Gate says "is this action permitted";
IFC says "is this *data* permitted to flow to this destination."

### The lattice (default set)

| Label | Tier | Model exposure | Examples |
|---|---|---|---|
| `§SECRET` | 0 | **Opaque handle only.** The model receives a reference (`attestation_id`, `credential_handle`, `secret:<hash>`) and never the bytes. | API keys, signing keys, PAN / card numbers, authority attestations, biometric templates, medical identifiers (HL7 PHI fields). |
| `§PII` | 1 | Shim-decided; model sees the data only with explicit registry opt-in. Default: redacted at ingress. | Personal data in scope of GDPR / CCPA / HIPAA personal identifiers. |
| `§CONF` | 2 | Model sees the data; egress to external destinations denied without explicit opt-in. | Business-confidential, unreleased-product, source code on private repos, internal project docs. |
| `§PUBLIC` | 3 | Full exposure. | Public API responses, public documentation, user-supplied prompt content absent other labels. |

**Ordering:** `§SECRET < §PII < §CONF < §PUBLIC` (lower tier is
stricter). The lattice's *join* operation takes the minimum (strictest)
label of its inputs — the standard information-flow rule. The shim's
canonical implementation is `tier = min(input_tiers)`.

### `validate [classify]` verb

```
[validate classify on=<ref>]
  destination:  <destination-tag>    # client | upstream-model | dispatch:<id> | memory-scope | ...
  permitted_max_tier: <tier>         # 0 | 1 | 2 | 3 — destination's accepted tier floor
  action_on_deny: <block | redact | escalate-to-gate>
[/validate]
```

`validate [classify]` evaluates the referenced artefact's current
label against the destination's `permitted_max_tier`; artefacts below
the tier are blocked, redacted, or escalated. The default
shim behaviour installs `validate [classify]` implicitly at every
egress boundary — client response, upstream-model call, dispatch
fan-out, memory commit, external tool invocation. Explicit
`validate [classify]` invocations override the default with a
tighter policy.

### Label propagation rule

Every artefact produced by the shim carries a `label` field equal to
the *tier-min* of its inputs' labels. The rule is mechanical and
implemented once in the shim; the model does not annotate outputs
with labels.

**Propagation surfaces:**
- **Wire compression** — output inherits the label-join of the input source.
- **Semantic retrieval** — query results inherit the label-join of the documents matched.
- **DRAW output** — every emission inherits the label-join of its input context (diagram summarising §PII data → §PII diagram).
- **Dispatch fan-out** — each sub-task inherits the parent turn's label-join plus the tier-floor of its destination.
- **Memory commit** — memory entries inherit the label-join of the turn that wrote them.
- **Converge telemetry** — inherits the label-join of all observed artefacts across the loop's lifetime.
- **TRACE entries** — carry the label of the action they record (but the trace itself is typically stored at the deployment's audit tier).

Propagation is monotone: no operation *lowers* a label. Explicit
declassification exists only as a named shim operation
(`declassify.<name>`) that requires a `policy` gate and an
`informational` audit gate; it is never initiated by the model.

### §SECRET opaque handles

§SECRET artefacts are handled under a stronger discipline than the
other tiers: the model *never* receives the bytes, only a handle.

**Handle shape:** `§SECRET[<kind>:<id>]` — for example
`§SECRET[credential:c4f1a0]`, `§SECRET[attestation:a8e2b7]`.

**Permitted operations on handles (model-visible):**
- **Reference** — include in structured output (gates, dispatches, validate invocations).
- **Probe metadata** — ask the shim for the handle's `kind`, `scope`, and `issuer` (never `bytes`).
- **Bind** — supply the handle as a field of a gate / dispatch / authority-attestation form.

**Prohibited operations (shim-enforced):**
- **Reason about bytes** — the model cannot request byte content; the shim refuses and logs the probe.
- **Round-trip** — the model cannot emit bytes for a §SECRET handle back into context. This matches the PCI tier-0 posture (merchant never holds the PAN) and the authority-gate posture (model never holds the fire-authority).

Tier-0 handles are the structural fix for the "prompt-injection
leaks credentials" class of failure: the credential was never in the
model's context to leak.

### Integration with Gate

A `policy` gate may condition on label: `if input.label > §CONF
then deny`. The default policy-gate configuration denies any egress
whose label exceeds the destination's declared tier. Authority
attestations are themselves §SECRET-classified; the authority gate's
integration with IFC (§Gate → `authority` integration point) is a
direct consequence of this.

### Integration with TRACE

Every TRACE entry carries a `label` field equal to the label of the
action's output. Audit backends may be tiered: a §SECRET-classified
trace entry may be delivered only to the deployment's highest-tier
audit store (hardware security module, split-secret storage,
sealed-log service), while lower-tier entries flow to standard
observability pipelines.

### Integration with DRAW (forward-referenced in v5.14)

Every DRAW emission carries the label-join of its input context.
The shim's render pipeline inspects the label and may degrade or
refuse rendering for high-classification content (e.g., refuse to
raster-synthesise a diagram labelled §SECRET; refuse to save a
§PII-labelled sketch to public memory). The `alt` field on DRAW
emissions is classified at the same label as the rendered content.

### Declarative extension

Deployments may extend the lattice with domain-specific labels
(e.g., `§HIPAA-PHI`, `§EXPORT-CONTROLLED`, `§TIER-0-CLIENT-DATA`).
New labels declare their position in the lattice (parent tier) and
their propagation rule. Registry validation rejects lattices that
are not partial orders or that re-use a canonical label name.

### Registry fields

| Field | Default | Notes |
|---|---|---|
| `ifc_labels` | `[§SECRET, §PII, §CONF, §PUBLIC]` | Lattice declaration; extensions append. |
| `ifc_default_destination_tier` | `§CONF` for client egress; `§PUBLIC` for upstream model | Per-destination default. |
| `ifc_declassify_requires` | `policy + informational` | Gates required on every declassify operation. |
| `ifc_audit_tier_map` | `{ §SECRET: hsm, §PII: regulated, default: standard }` | Where TRACE entries are delivered by label. |

---

## Protocol Compliance Adaptation

The spec now formally handles the full cross-product of client
and model compliance. Either end can be stock (unaware of Ozma),
partially compliant, or fully compliant, and the shim is
responsible for making the pairing produce useful behavior in
every quadrant — including the "neither compliant" case where
Ozma acts as value-add infrastructure on an otherwise opaque
Token Model proxy.

### Compliance tiers

**Client tiers:**

| Tier | Name | What it means |
|---|---|---|
| C0 | Stock | Plain HTTP; OpenAI-compatible chat completions, Anthropic messages, Gemini generateContent, LiveKit room, Daily room, raw WebSocket chat, generic curl. No Ozma headers. |
| C1 | Partial | Some Ozma headers (rendering caps / persona) but not structured parsing. Typically a legacy app being incrementally upgraded. |
| C2 | Full | Full Ozma protocol participant. Sends full header set, parses structured blocks, renders all capability types natively. |

**Model tiers:** L0 / L1 / L2 (as specified in §Capability Levels),
plus the v5.3 self-upgrade mechanism that lets a stock L0 or shallow-
retrofit L1 model be taught selected protocol features at runtime.

### The four quadrants

```
                          MODEL
               stock (L0)   protocol-aware (L1 retrofit / L2)
              ┌────────────────┬────────────────────┐
              │                │                    │
  stock (C0)  │   Passthrough  │  Rendering shim    │
              │   + value-add  │  (strip structure) │
              │                │                    │
              ├────────────────┼────────────────────┤
              │                │                    │
  Ozma (C2)   │  Teaching      │  Native            │
              │  self-upgrade  │  (full protocol)   │
              │                │                    │
              └────────────────┴────────────────────┘
                         (C1 blends these)
```

**Quadrant behavior:**

| Quadrant | Shim responsibility |
|---|---|
| **C0 × L0** (neither compliant) | Passthrough with value-add. Pure proxy: routing, caching, audit, rate-limit, cost accounting, security scanning, prompt hints (from the rules engine). No structured transforms; no persona override. The shim earns its keep via ops features, not protocol features. |
| **C0 × L1 / L2** (stock client, protocol-aware model) | Rendering shim. Strip structured blocks from output; render rich content (HTML/SVG/VRM/SSML) into the client's declared formats via transform bridges. Persona, memory, rules all active server-side but invisible to the client. |
| **C2 × L0** (Ozma client, stock model) | Teaching mode. Inject self-upgrade teaching prompts (v5.3) so the stock model emits best-effort Ozma structure. Parse output, coerce into structured form where possible, fall back to rich-parser when not. Client still sees full Ozma responses. |
| **C2 × L1 (retrofit-trained) / L2** (both compliant) | Native. Full protocol, no transforms required. Fast path. |
| **C1 mixes** | Treated per feature: if the client declares `X-Ozma-Persona` but not `X-Ozma-Render`, persona applies but rich output is sent verbatim. Each header is independent. |

### Client adapters

Client-side compliance is discovered in one of two ways:

1. **Explicit** — client sends `X-Ozma-Client-Adapter: <name>`
   or the Ozma header surface.
2. **Implicit** — the shim inspects the request shape (path,
   Accept, body schema) and matches a file in
   `ozmalib/client_adapters/*.yaml`.

**Adapter files** declare how to map an incoming request into
Ozma's canonical envelope, and how to map Ozma output back into
the client's expected response shape.

```yaml
# ozmalib/client_adapters/openai-chat-completions.yaml
id: openai-chat-completions
tier: C0
detect:
  path: [/v1/chat/completions]
  content_type: [application/json]
  body_has: [model, messages]
request_map:
  model: ozma.model_id
  messages: ozma.turns
  temperature: ozma.sampling.temperature
  tools: ozma.tool_schemas
  stream: ozma.stream
response_map:
  ozma.body.text: choices[0].message.content
  ozma.body.tool_calls: choices[0].message.tool_calls
  ozma.telemetry.tokens: usage
  ozma.telemetry.finish_reason: choices[0].finish_reason
capabilities:
  client_caps: [plain]                  # C0 = plain unless inferred otherwise
  accepts: [text/plain, application/json]
  rich_output: strip                    # rendering-shim mode
```

Shipped adapters (initial set):

| Adapter | Tier | Target |
|---|---|---|
| `openai-chat-completions` | C0 | OpenAI / OpenAI-compatible |
| `openai-responses` | C0 | OpenAI Responses API |
| `anthropic-messages` | C0 | Anthropic Messages API |
| `google-generate-content` | C0 | Gemini API |
| `livekit-room` | C0 | LiveKit real-time rooms |
| `daily-room` | C0 | Daily real-time rooms |
| `websocket-chat` | C0 | Generic JSON-over-WebSocket chat |
| `curl-basic` | C0 | Bare curl (text/plain in, text/plain out) |
| `ozma-native` | C2 | Full Ozma |
| `ozma-native-partial` | C1 | Negotiated per declared header set |

### Model adapters

Symmetric to client adapters: how to speak to the upstream model.

```yaml
# ozmalib/model_adapters/openai-chat-tools.yaml
id: openai-chat-tools
level: 0
detect:
  model_id: [gpt-4o, gpt-4o-mini, gpt-4-turbo, o1, o3]
upstream:
  endpoint: https://api.openai.com/v1/chat/completions
  auth: env:OPENAI_API_KEY
request_map:
  ozma.turns: messages
  ozma.tool_schemas: tools
  ozma.sampling.temperature: temperature
  ozma.sampling.max_tokens: max_tokens
response_map:
  choices[0].message.content: ozma.body.text
  choices[0].message.tool_calls: ozma.body.tool_calls
  usage: ozma.telemetry.tokens
teaching_prompts:                        # v5.3 self-upgrade
  enable: true
  caps: [OUTLINE, EDIT, STYLE, THEME]    # selective teaching
  budget_tokens: 800
```

Shipped adapters (initial set):

| Adapter | Tier | Target |
|---|---|---|
| `ozma-native` | L1 (full retrofit) / L2 | First-party Ozma models |
| `openai-chat-tools` | L0→L1 `prompted` (taught) | OpenAI + OpenAI-compatible |
| `openai-responses` | L0→L1 `prompted` | OpenAI Responses API |
| `anthropic-messages-tools` | L0→L1 `prompted` | Anthropic Messages |
| `google-generate-content` | L0→L1 `prompted` | Gemini |
| `completion-api` | L0 | Bare completion endpoints (legacy) |
| `local-llama-cpp` | L0→L1 `prompted` | llama.cpp server |
| `hf-inference` | L0 | HuggingFace Inference API |
| `ollama` | L0→L1 `prompted` | Ollama local |
| `vllm-openai` | L0→L1 `prompted` | vLLM OpenAI-compatible |

Model-adapter `teaching_prompts.enable: true` opts the adapter
into v5.3 self-upgrade when paired with a C1/C2 client. The
`caps` list is the subset the shim will teach; full teaching
carries a token tax that makes it unattractive for every request.
Empirically: teach 3–5 capabilities per turn, rotated by need.

### X-Ozma-Mode header

A single client header selects the adaptation strategy:

| Value | Behavior |
|---|---|
| `default` | Full adaptation in both directions. Shim picks the appropriate adapters and teaches / strips as needed. This is the recommended setting. |
| `passthrough` | Minimal shim behavior: routing, caching, audit, security, cost accounting only. No teaching, no structure parsing. Useful for debugging and pure proxy deployments. |
| `force-protocol` | Reject mismatched pairs. If the client is C0 or the model is L0 without teaching support (no UPGRADE ack, not instruction-following), fail with `compliance_downgrade`. Used in testing to prove the protocol path is exercised. |

### Adapter selection algorithm

```python
def select_adapters(request, registry):
    # 1. Client adapter — prefer explicit header, else detect
    client_adapter = (
        registry.client_adapters.get(request.headers.get("X-Ozma-Client-Adapter"))
        or match_by_shape(request, registry.client_adapters)
    )
    # 2. Model adapter — keyed on upstream model id
    model_adapter = registry.model_adapters.for_model(request.target_model)
    # 3. Mode-based gating
    mode = request.headers.get("X-Ozma-Mode", "default")
    if mode == "force-protocol":
        assert_or_raise(client_adapter.tier >= C1, "compliance_downgrade")
        assert_or_raise(model_adapter.level >= 1 or model_adapter.teaches, 
                        "compliance_downgrade")
    elif mode == "passthrough":
        client_adapter = client_adapter.passthrough_variant()
        model_adapter = model_adapter.passthrough_variant()
    return client_adapter, model_adapter
```

The selection is cached per session (client id × model id ×
mode) since it rarely changes within a session.

### Response headers

The shim echoes the adaptation decision so the caller can see
what happened:

```
X-Ozma-Client-Tier: C0
X-Ozma-Model-Tier: L0
X-Ozma-Adapter-Applied: openai-chat-completions + openai-chat-tools
X-Ozma-Teaching-Active: OUTLINE,EDIT,STYLE
```

### Observability

Telemetry records the applied pair per turn:

```jsonc
{
  "client_tier": "C0",
  "client_adapter": "openai-chat-completions",
  "model_tier": "L0",
  "model_adapter": "openai-chat-tools",
  "mode": "default",
  "taught_caps": ["OUTLINE", "EDIT", "STYLE"],
  "teaching_tokens": 760,
  "quadrant": "C0xL0"
}
```

This lets operators answer "how many turns in my traffic are
native Ozma vs. value-add vs. teaching-mode?" — a question every
deployment cares about.

---

## Model-Directed Dispatch

The protocol's meta-thesis — *context is the program, better data
produces better decisions* — applied to routing. Classifier
heuristics that guess difficulty from prose are replaced by
planner-emitted `DISPATCH` directives grounded in real routing
data. The classifier remains as the fallback path for C0/L0
pairings where no planner exists.

### The ten-strategy family

One capability (`DISPATCH`) covers ten dispatch shapes. Each
maps to a distinct code path in the shim; strategies are not
open-vocabulary. See §DISPATCH for per-strategy fields and
examples. Summary:

| Strategy | One-liner |
|---|---|
| `cheapest-capable` | Pick the lowest-tier model that meets the quality floor. Default. |
| `cascade` | Try cheap; escalate when confidence / claim-verification fails. |
| `race` | Parallel N; accept fastest valid. For urgency. |
| `vote` | Parallel N; majority / consensus. For subjective decisions. |
| `blend` | Parallel N; synthesize the best traits from each. LLM-Blender pattern. |
| `ensemble` | Parallel N; present all outputs as options. For generative diversity. |
| `split` | Decompose first, then dispatch sub-parts. Depth cap default 2. |
| `verify` | N-model hypothesis verification. Confidence-weighted. |
| `human-gate` | Pause for approval. Destructive / high-blast-radius. |
| `self` | Decline to dispatch; do inline (for audit trail). |

### Three-axis budget envelope

Token cost is not the only constraint. Every DISPATCH carries
three orthogonal budgets:

| Axis | Field | Guards against |
|---|---|---|
| Money | `budget.cost_usd_max` | Overspend on model-API tokens. |
| Wall-clock | `budget.latency_ms_max` | Slow responses. |
| Side-effects | `budget.side_effects.{ci_minutes_max, external_calls_max, tokens_tree_max}` | Uncapped real-world work (CI runs, external API calls, cumulative token fan-out across the dispatch tree). |

Anthropic's 2026 implementation guide documents the three axes
(`max_wall_clock_minutes`, `max_ci_minutes`, `max_model_cost_usd`)
— we formalize that pattern as the normative envelope.

### Cost attribution — solving the open problem

Every dispatch propagates a **trace** that composes nested
dispatches into a single per-turn rollup. Anthropic's own 2026
report: *"Multi-agent workflows generate unpredictable token
costs. Claude Code currently offers no native cost tracking by
user, team, or project."* v5.8 makes this the foundation:

- `trace_id` — W3C Trace Context style; stable across the top-
  level user turn and every descendant dispatch. Standard 16-byte
  hex id.
- `parent_dispatch_id` — direct parent pointer; forms the dispatch
  tree for any turn.
- `span_id` — unique per dispatch invocation.
- `rollup_cost_usd`, `rollup_latency_ms`, `rollup_side_effects` —
  cumulative across the tree, emitted at turn end in a
  `[rollup-ledger]` block plus `X-Ozma-Rollup-*` response
  headers.
- `rollup_ledger` — per-dispatch structured breakdown (strategy,
  target, realized cost, realized latency, outcome) so one turn
  that fans out to 12 descendants still reports one total with
  full attribution drill-down.

### Strategy-quality telemetry

Per-strategy fields in the rollup that answer "did this strategy
earn its multi-target cost?":

| Strategy | Field | Interpretation |
|---|---|---|
| `race` | `race_realized_vs_expected_latency` | Ratio of actual best-latency vs. median-latency of the field. ≥0.7 means the race saved meaningful time. |
| `vote` | `vote_disagreement_rate` | Fraction of targets that disagreed with the winner. Low → consensus held (vote was cheap insurance); high → the ground truth was genuinely contested. |
| `blend` / `ensemble` | `diversity_score` | Cosine-distance-based measure of how different the N outputs were. Low → you paid N× for near-duplicates. |
| `cascade` | `escalation_rate`, `escalation_success_rate` | How often the cheap tier failed; how often the escalation succeeded. Informs cheap-tier selection and cascade gate tuning. |
| `verify` | `agreement_mean`, `agreement_variance` | Confidence-weighted agreement across N verifiers; variance signals ambiguity. |

### Human-gate fatigue mitigation

Per Anthropic's empirical finding: **93% of Claude Code
permission prompts are approved; 17% of genuinely destructive
actions are false-negatives in the safety classifier.** Naive
HITL gates inherit approval fatigue. The spec mandates four
controls on `human-gate`:

- `rising_threshold: N` — after N incident-free approvals of
  structurally identical gates, auto-approve (N+1) with
  telemetry flag `auto_approved_by_prior`. Resets on any denial.
  Default `null` (every gate requires approval).
- `cool_down_seconds: M` — mandatory re-read time before an
  approval button becomes active. Anti-reflexive-click. Default
  `0` for low-stakes, `15` for destructive, configurable.
- `batch_similar: true` — structurally similar pending gates
  grouped into one review surface. Reduces context-switch cost
  without reducing consideration per decision.
- `explain_on_deny: required` — denial requires a text reason.
  Reasons feed back into future-turn dispatch rationale as
  negative examples.

`approver_rules` supports multi-stakeholder projects:
`{rule: any_owner}`, `{rule: all_owners}`, `{rule: from_team,
team: platform}`, `{rule: named, id: matt}`.

### Compliance policy enforcement

If WORK-UNIT declares `dispatch_policy.compliance: [hipaa,
us-only]`, the shim filters `available_models` in
`ROUTING-CONTEXT` **before** injection. The model never sees
non-compliant models; attempts to explicit-target a disallowed
model raise `dispatch_policy_violated`. Data-layer enforcement,
not model-discretion enforcement — regulated data combined with
a 17% classifier error rate requires the safer layer.

### Dev-test-review as a documented pattern

v5.8 does not add a new primitive for verification loops — the
existing composition of `PLAN` + `DISPATCH` + `CLAIM` + `AGENT-
STATE` already expresses them. The spec documents the canonical
shape so training data, teaching prompts, and deployments
converge:

```
plan:
  step s1 dev    [dispatch target=ozma-coder-2 strategy=cheapest-capable]:
    implement per WORK-UNIT step-s2 design
  step s2 test   [dispatch strategy=self]:
    run the test suite via the dev-loop tool
  step s3 review [dispatch strategy=verify n=5 confidence_floor=0.9]:
    hypothesis: "the refactor preserves semantics on all inputs satisfying @invariant_set"
  step s4 fix    [dispatch target=ozma-coder-2 strategy=cascade escalate_to=ozma-wizard-1]:
    fix the failing invariants reported by step s3
    precondition: { step: s3, confidence_below: 0.9 }
```

Key properties:

- **Each phase is a dispatch.** Routing, budgets, and enrichment
  apply uniformly — test runners and review verifiers are just
  targets.
- **Phases compose via AGENT-STATE.** Step s3's output becomes
  s4's context via `precondition` and AGENT-STATE's
  progress-tracking, not via bespoke wiring.
- **Verification is an integrated command.** What was a
  hand-rolled script ("run this assertion across 20 models and
  aggregate") becomes one `strategy=verify n=20` dispatch.
  Correlated-error reduction gives dramatically lower ε than
  single-model checks.
- **Budget shapes the loop.** Increase `verification_rigor`
  scale on the WORK-UNIT → step s3's default `n` rises and its
  `confidence_floor` tightens. One preference, whole loop
  recalibrates.

### Context enrichment — orchestrator → sub-worker

The orchestrator holds the big picture and the sub-task context;
it can pre-digest a sub-task so a cheaper model suffices. Every
DISPATCH supports `context_enrichment` with five fields
(`task_summary`, `examples`, `constraints`, `pitfalls`,
`difficulty_estimate`) and a scale `enrichment_level: 0.0–1.0`
on WORK-UNIT that sets the default strength.

Auto-derivation when unset: `enrichment_level = 1 -
cost_vs_quality`. A user who prioritizes cost (`cost_vs_quality
= 0.2`) gets `enrichment_level = 0.8` by default — orchestrator
pays a one-time planning cost so many small dispatches run
cheap. A user who prioritizes quality (`cost_vs_quality = 0.9`)
gets `enrichment_level = 0.1` — let the big model reason from
first principles.

Makes the economic tradeoff explicit and tunable where today it
is hidden in prompt templates.

### IO semantics summary

| Concern | Default | Override |
|---|---|---|
| Stream | `interleave` with `[dispatch-stream id=d:N]` frames | `X-Ozma-Dispatch-Stream: buffer \| separate-channel` |
| Cancel | `[dispatch:cancel id=d:N]` propagates to descendants | Shim-emitted on user abort |
| Provenance | `[from=d:N]…[/from]` tagged spans | `X-Ozma-Provenance: on \| off`; default on for full-retrofit L1 and L2 |
| Context inheritance | `context_required` explicit refs only | Parent context *not* inherited — cheaper and matches Anthropic's subagent model |

### Shim-execute vs client-execute

`X-Ozma-Dispatch: shim-execute | client-execute | forbid`:

- `shim-execute` (default) — shim routes sub-tasks transparently;
  client sees the rolled-up result with provenance tags.
- `client-execute` — shim forwards the DISPATCH directive to the
  client; client routes through its own infrastructure and
  returns results. Appropriate when the client has private model
  access the shim doesn't.
- `forbid` — DISPATCH directives in model output are advisory
  only; nothing is routed. Used in dry-run and training-data
  generation contexts.

---

## Work Units and Project Management

Three tiers of durability, one capability, consistent grammar
family. Matches MEMORY's scope pattern (session / workspace /
user) applied to task structure.

### The three tiers

See §WORK-UNIT for the full grammar. At a glance:

| Tier | Trigger | Scope | Persistence | External backing |
|---|---|---|---|---|
| `task-list` | Informal request: "fix these three things" | session | LRU-evicts at session end (unless referenced ≤ 3 turns) | none |
| `mini-project` | Complex request spanning turns/files: "build this feature" | workspace | 90-day idle expiry; re-touch extends | none |
| `project` | Explicit PM reference: "for MOB-487", "linear:proj-abc" | external PM system | owned by external system | yes |

### Auto-tier selection

When no `X-Ozma-WorkUnit-*` header is set:

1. **Model-driven** — if this turn's model output includes a
   `[work-unit tier=...]` block, use that tier. The planner has
   judged what durability the task warrants.
2. **Rule-based prior** — single-file single-turn task →
   `task-list`; multi-file or multi-turn → `mini-project`;
   explicit PM reference (`for MOB-487`, `linear:...`, etc.) →
   `project`.
3. **No work unit** — plain turn, no WORK-UNIT created.

Model-driven wins; rules are fallback, consistent with the
whole v5.8 premise.

### Promotion

Tier ↑ only, never ↓. Promotions are durability upgrades, not
demotions:

- `task-list → mini-project` — shim persists the current
  contents as a workspace-scoped WORK-UNIT. Triggered by model
  emitting `[work-unit:promote from=tl:... to=mini-project]` or
  by rule ("task-list referenced in ≥ 3 turns → auto-promote").
- `mini-project → project` — shim invokes the configured PM
  adapter to create an external ticket. Opt-in per adapter (see
  PM adapter write scope below). Side-effect: the new project-
  tier WORK-UNIT carries the mini-project's history as
  `paused_reason: "promoted from mini-project mp:..."`.

Demotion is not supported — a project is not "downgraded" to a
mini-project; instead it may be closed in the PM system, which
expires the WORK-UNIT normally.

### PM adapters

`ozmalib/pm_adapters/*.yaml` — each adapter declares:

- `id` — `linear | jira | github-issues | asana | notion |
  clickup | ozma-native | mcp-passthrough | ...`
- Authentication and endpoint configuration
- `read_scope` — which PROJECT-CONTEXT fields this adapter can
  populate (project metadata, ticket list, sprint info, velocity)
- `write_scope` — which hooks this adapter may fire on
  completion. Default `[]` (read-only); writes opt-in per
  adapter.
- `capability_mapping` — per-adapter translation of the WORK-
  UNIT schema to the PM system's native schema (priority
  mapping, status mapping, custom-field mapping)

**Shipped adapters (initial set):**

| Adapter | Backing | Initial scope |
|---|---|---|
| `linear` | Linear (GraphQL) | Native — full read, write opt-in |
| `jira` | Atlassian Jira via Remote MCP | Full read, write opt-in |
| `github-issues` | GitHub Issues API | Full read, write opt-in |
| `asana` | Asana API | Read-only default |
| `notion` | Notion API | Page-as-ticket; read-only default |
| `clickup` | ClickUp API | Read-only default |
| `ozma-native` | Ozma's own PM surface | Full read-write |
| `mcp-passthrough` | Any MCP server | Read + write per MCP server's declared tools |

**`mcp-passthrough` as the meta-adapter.** Since MCP has become
the de-facto standard for Model↔tool integration (Atlassian Remote
MCP, Linear MCP, GitHub Issues MCP, and the rest of the
awesome-mcp-servers catalog), `mcp-passthrough` wraps any
existing MCP server. One adapter, unlimited ecosystem reach.

### Workspace policy

`workspace_policy: persistent | ephemeral | sandboxed` — how
side-effects persist across dispatches:

- `persistent` — working state preserved across dispatches and
  across sessions (Devin-pattern). Sub-dispatches share a
  workspace VM; edits on step s1 are visible to step s4.
- `ephemeral` — fresh environment per dispatch (Codex-pattern).
  Sub-dispatches see only what they received via
  `context_required`; changes don't leak.
- `sandboxed` — isolated; no external side-effects at all (no
  writes, no network). For proposal / dry-run modes.

### Pause / resume

Long-running projects may be paused for days. Three fields:

- `paused_at: <iso>` — when the pause started
- `paused_reason: "..."` — free-text reason (surfaced to resume
  turn for continuity)
- `expected_resume: <iso>` — for monitoring and reminders

On resume, the shim injects both the PROJECT-CONTEXT (fresh from
PM adapter) and an AGENT-STATE delta documenting what was in
flight when the pause began. Cross-session continuity without
re-deriving state from commit history.

### Code ↔ project linkage

`code_refs: [ { repo, branch, commits?, files? } ]` on every
WORK-UNIT. Lets the model see which branches and files belong
to which ticket without re-deriving from git archeology. Linear's
MCP already surfaces this for GitHub linkage; we normalize
across adapters.

### `on_complete` hooks

Declarative side-effects when WORK-UNIT reaches `status=done`.
Hooks run through the configured PM adapter and/or shim
extensions. All writes opt-in at the adapter level:

```yaml
on_complete:
  - pm-adapter:close
  - pm-adapter:comment: "Completed by Ozma session {{session_id}}; diff: {{diff_url}}"
  - notify:matt@slack
  - notify:#platform@slack
  - webhook: { url: "https://...", method: POST }
  - promote: { target_workunit: mp:sibling, to_tier: project }
```

### PROJECT-CONTEXT redaction for training

Project context can contain deadlines, team names, internal
constraints, customer references. Sessions with
`X-Ozma-Training: opt-in` run a redaction pass before training-
data ingestion. Rule category `project-context-redaction`
defines field-level allowlist/denylist and regex scrubs. Default
configuration redacts `team[*].email`, `owner.email`, external
URLs containing customer identifiers, and free-text fields
tagged `sensitive: true`.

### Research grounding

- **Anthropic 2026 Agentic Coding Trends Report** — five-layer
  stack (MCP / Skills / Agent / Subagents / Agent Teams); cost-
  governance as open problem that v5.8 solves.
- **Cognition Devin 2.0 Interactive Planning** — persistent
  workspace pattern informing `workspace_policy: persistent`.
- **Codex CLI ephemeral sandboxes** — ephemeral pattern informing
  `workspace_policy: ephemeral`.
- **Atlassian Remote MCP Server, Linear MCP, GitHub Issues MCP,
  awesome-mcp-servers catalog** — grounding for the adapter
  ecosystem and `mcp-passthrough` as meta-adapter.
- **LangChain, Temporal, Microsoft Agent Framework HITL** — human-
  in-the-loop patterns informing the `human-gate` strategy.
- **Anthropic Claude Code empirical findings** — 93% approval
  rate, 17% destructive-action false-negative rate — motivating
  rising-threshold + cool-down + explain-on-deny.

---

## Peer-to-Peer Coordination

Peer coordination is graph-shaped, orthogonal to v5.8's tree-shaped
DISPATCH. Agents maintain independent sessions, exchange typed
messages, and coordinate via blocker graphs, claims, channels, and
subscriptions — the way a human team does. This is Anthropic's
Agent Teams layer addressed at the protocol level rather than at
per-framework level.

### Transport: native default, A2A as interop adapter

**Default transport is `ozma-native`** — Ozma grammar over HTTPS.
All existing compression primitives (Session Cache `{@agent:HASH}`
for Agent Cards, WIRE for prose bodies) apply natively without any
envelope-mapping layer; typical peer message is 60–120 tokens
end-to-end. Message type bodies follow the typed schemas in
§COORDINATION.

A2A and other standards are available as interop adapters via the
unified §Adapter primitive with `kind=agent-transport`:

| Transport | Use |
|---|---|
| `ozma-native` (default) | Ozma grammar over HTTPS; native compression applies |
| `a2a` | Google A2A v1.0 / JSON-RPC — interop with the 150+ org A2A ecosystem (lossy translation; see below) |
| `a2a-grpc` | A2A over gRPC — binary network wire for bandwidth-constrained paths (still lossy for coordination semantics) |
| `mcp-agent-mail` | Git-backed mailbox with file reservations — local/offline deployments |
| `amq-file` | Maildir-style local file queue — zero-infrastructure dev/test |

Transport is selected per deployment via
`X-Ozma-Coordination-Transport`; falls back to a common-denominator
transport if the preferred is unreachable (logged).

### A2A translation loss (when using the A2A adapter)

A2A's shape is strictly lossier than native Ozma for coordination
semantics. The adapter must declare what cannot be faithfully
expressed:

| Native Ozma concept | A2A representation | Loss |
|---|---|---|
| `handoff` message with structured `accepting_criteria` | A2A Task with Parts-blob body | Criteria flatten to free-text; reviewer cannot validate schematically |
| `claim` on file scope | metadata on Task | Scope semantics not honored by non-Ozma peers; claims become advisory-hint only |
| `block` / `unblock` with edge reference | Task state `INPUT_REQUIRED` / resumption | Blocker graph not representable; cascade unblocks not cross-adapter |
| `review-purpose request` + `response verdict` | Task with Artifact | `verdict: lgtm \| changes-requested \| abstain` collapses to artifact content |
| `subscribe` to channel / work-unit | No representation | Subscriptions are not cross-adapter; adapter maintains local-only mirror |
| Priority floor filtering | No representation | Receiver gets full firehose; must filter locally |
| WORK-UNIT blocker-graph edges | No representation | Dependency metadata lost on egress |
| Agent `compliance` tags | Agent Card extension (custom) | Non-Ozma peers may ignore |

The A2A adapter surfaces these losses via
`X-Ozma-A2A-Translation-Loss: <summary>` on outbound traffic. For
round-trip semantics preservation, both peers must run Ozma
native; A2A is for egress to non-Ozma ecosystems.

### Native compression is free

Because the default transport is already Ozma grammar, there is no
separate compression envelope layer to maintain. The existing caps
apply directly:

- **Session Cache** (`{@agent:HASH}`) — Agent Cards content-
  addressed; first turn sends full card + hashes it; subsequent
  turns reference by hash. Signatures preserved as hash + sig pair.
- **WIRE** — prose in message bodies (subjects, reasons, body
  fields, descriptions).

No peer-pair teaching of a compression table is required for
native-to-native traffic — both ends already speak Ozma grammar.
Teaching is only relevant when talking to a capable peer over the
A2A adapter who could be upgraded to native (see §Runtime
Teaching — the shared mechanism for shim→model, model→client,
agent→peer-agent cases).

### Hardening

Addresses documented multi-agent failure modes (MAST taxonomy 2026:
42% bad specs / 37% coordination breakdowns / 21% weak
verification). v5.9 targets the 37% coordination bucket.

**Handoff depth cap.** Per thread (not per session), default 3
(configurable via `X-Ozma-Handoff-Depth-Max`). On cap, the next
handoff auto-escalates to `human-gate` (via v5.8 DISPATCH
strategy). Prevents the documented "agents bounce tasks back and
forth without resolution" failure mode.

**Oscillation detector.** Shim tracks handoff sequences per
thread; detects A→B→A→B patterns regardless of depth and forces
escalation to `human-gate` on the second oscillation. Ignores
single round-trips (A→B→A is often normal clarification).

**Explicit acceptance.** `handoff` messages require `requires_ack:
true`. Receiver must emit `[message type=ack in_reply_to=m:42
accepted=true]` or `accepted=false`. A `false` ack counts toward
depth (cannot silently bounce).

**Per-agent-pair rate limits.** Default 60 messages per 5-minute
rolling window per (from, to) pair. Configurable via
`X-Ozma-Message-Rate-Limit`. Exceeding the limit raises
`message_rate_limit_exceeded`; excess messages are queued until the
window clears.

**Message dedup.** Message ids are 16-byte UUIDs (encoded as
`m:<hex16>`). Shim deduplicates on id across direct-delivery and
channel-fanout paths — a message sent both directly and via a
subscribed channel is delivered once.

**Compliance filtering.** Messages carry optional
`compliance: [us-only, soc2]` tags. Transport-layer filter: the
shim refuses delivery to peers whose identity does not satisfy the
tag set. Same pre-injection-filter pattern as v5.8
`dispatch_policy`.

### Inbox delivery and wake

On each agent turn start, shim injects a filtered `[inbox]` block.
Filters applied in order:

1. **Subscription match** — only channels / work-units / event types
   the agent has subscribed to.
2. **Priority floor** — `priority >= subscription.priority_floor`.
3. **TTL** — messages past their TTL are expired (default workspace
   90-day, matching mini-project WORK-UNIT).
4. **Dedup** — one copy per message id regardless of delivery path.
5. **Compliance** — recipient's compliance tags satisfy message
   tags.

Filtered inbox is compact — id + type + from + subject + priority.
Full bodies fetched on demand via `[message:fetch id=m:42]`
sub-block (returned in next tool response, same pattern as session
cache).

**Wake modes**: `passive` (default — turn-based) vs
`message-triggered` (opt-in — shim wakes the agent on priority-
floor-crossing messages). `message-triggered` is declared in the
agent card's `wake_policy` and requires explicit deployment
authorization (compute + cost implications).

### Composition with existing capabilities

The normative composition table for COORDINATION + HANDOFF + DISPATCH +
WORK-UNIT + SCHEDULE distinctions is in §COORDINATION above. When the two
drift, the §COORDINATION copy wins — this section is a pointer, not a
second source of truth.

### Research grounding

- **Google A2A v1.0** (Linux Foundation, 150+ organizations) —
  transport floor.
- **OpenClaw teams RFC** — shared task list with dependencies,
  blocked + claimed states, per-agent mailbox.
- **mcp-agent-mail** — git-backed mailbox + advisory file
  reservations (= our `claim` concept).
- **Agent Message Queue (AMQ)** — Maildir-style local file
  queue; zero-infrastructure reference for `amq-file` backend.
- **MAST taxonomy (2026)** — multi-agent failure analysis
  motivating hardening.
- **Anthropic 2026 Agentic Coding Trends Report** — Agent Teams
  as the five-layer stack's coordination layer.
- **FIPA-ACL (2000)** — historical performative lineage (inform,
  request, propose) informing our ten message types.
- **Ecma NLIP (Dec 2025)** — parallel Natural Language
  Interaction Protocol standard; adjacent work.

---

## Trace and Audit

One schema for correlation, cost attribution, audit, and
compliance-reporting across every action the shim mediates.
Subsumes v5.8 `rollup_ledger`, v5.5 motion audit log, v5.9
coordination message log, and proposed v5.10 emit/gate audit.

### Why unified

Before v5.9 the spec had parallel audit surfaces: `X-Ozma-Rollup-*`
for DISPATCH, `X-Ozma-Motion-Audit-*` for MOTION, scattered
message-log fields for COORDINATION, separate emit/gate log for
EMIT. Each had its own id namespace, storage, and response
headers. Consolidation:

- One `trace_id` spans a user turn and every descendant action
  (dispatch, motion, message, emit, gate evaluation).
- One schema for every audit entry → one log pipeline, one
  compliance-report query surface.
- One response-header family `X-Ozma-Trace-*` instead of four.
- Standard W3C Trace Context format — interoperates with
  distributed-tracing systems (OpenTelemetry, Honeycomb, Datadog,
  Grafana Tempo).

### Canonical schema

Every action across the protocol emits one or more trace entries:

```jsonc
{
  "trace_id":        "t:7fa1d8e2c4b93601",     // stable across the turn + all descendants
  "span_id":         "s:3a9c2e",                // unique per entry
  "parent_span_id":  "s:1e0b4a",                // root has null
  "kind":            "dispatch | motion | message | emit | gate | handshake | teaching | claim | block | transaction",
  "subkind":         "cheapest-capable | ros2 | handoff | slack-webhook | human | ...",
  "actor":           "agent:linear:MOB-487",    // agent emitting; null for shim-originated
  "target":          "ozma-coder-2 | front-cam | agent:MOB-488 | #platform-auth | stripe-charges",
  "workunit":        "linear:MOB-487",          // null if not scoped to a work-unit
  "started_at":      "2026-04-20T14:22:07.412Z",
  "completed_at":    "2026-04-20T14:22:08.091Z",
  "latency_ms":      679,
  "cost_usd":        0.00041,
  "side_effects":    { "ci_minutes": 0, "external_calls": 1, "tokens": 620 },
  "outcome":         "ok | deny | modify | delay | escalate | cancel | error | timeout",
  "decision_reason": "policy OPA approved",     // optional; populated for gate outcomes
  "refs":            ["{@message:m:42}", "{@dispatch:d:3}"],
  "compliance_tags": ["us-only", "soc2"],
  "privacy_tags":    ["contains-pii: false"],
  "identity_layer":  { "actor_signed": true, "card_fingerprint": "a4c299" },  // for audit (five-layer trail)
  "input_layer":     { "turn_id": "turn:42", "from_user_utterance": true },
  "reasoning_layer": { "rationale_ref": "{@span:s:1e0b4a}", "confidence": 0.92 },
  "action_layer":    { "operation": "chargeCustomer", "idempotency_key": "ch-2026-04-20-abc-001" },
  "outcome_layer":   { "status": "succeeded", "response_digest": "sha256:..." }
}
```

The five `*_layer` fields implement the **five-layer audit
trail** (identity / input / reasoning / action / outcome) that the
2026 industry guidance recommends for regulatory compliance.
Entries omit layers that don't apply to their `kind`.

### Tree shape

Trace entries form a tree rooted at the user turn. Parent/child
relationships encode causality:

```
t:7fa1 (user turn)
├─ s:1e0b  dispatch d:1 strategy=cheapest-capable (ozma-flash)
├─ s:1e0c  dispatch d:2 strategy=cascade (ozma-coder-2 → ozma-wizard-1)
│           ├─ s:2a1f  dispatch-escalation d:2.1 (ozma-wizard-1)
│           └─ s:2a20  gate g:1 kind=human approved
├─ s:1e0d  message m:42 type=handoff to=agent:MOB-488
│           └─ s:3f1a  message m:43 type=ack from=agent:MOB-488 accepted=true
├─ s:1e0e  emit e:1 type=slack-webhook
│           └─ s:4c2b  gate g:2 kind=informational sox-audit log-forwarded
└─ s:1e0f  rollup-summary
```

### Turn-end rollup

At turn end the shim emits a `[rollup-ledger]` block (grammar-
level, model-visible) that summarizes the trace tree:

```
[rollup-ledger trace_id=t:7fa1d8e2c4b93601]
  total:
    entries: 12
    cost_usd: 0.0421
    latency_ms: 8420
    side_effects: { ci_minutes: 0, external_calls: 3, tokens: 42019 }
  by_kind:
    dispatch: 4  cost_usd=0.031  latency_ms=6200
    message:  3  cost_usd=0.002  latency_ms=120
    emit:     2  cost_usd=0.008  latency_ms=1800
    gate:     3  cost_usd=0.001  latency_ms=300
  strategy_quality:
    cascade_escalation_rate: 1.0
    verify_agreement_mean: null
[/rollup-ledger]
```

This `[rollup-ledger]` is the model-visible summary; the full
structured trace entries are in telemetry (§Telemetry Schema) and
in the deployment's chosen trace backend (configurable via the
unified `Adapter` primitive, kind=`trace-backend` — OpenTelemetry,
Honeycomb, Datadog, etc.).

### Response headers (unified family)

Before v5.9 response headers were scattered across three families
(`X-Ozma-Rollup-*`, `X-Ozma-Motion-Audit-*`, `X-Ozma-Coordination-*`).
v5.9 collapses to one `X-Ozma-Trace-*` family:

| Header | Effect |
|--------|--------|
| `X-Ozma-Trace-Id` | W3C-Trace-Context-compatible id spanning this turn and every descendant action. |
| `X-Ozma-Trace-Entries-Count` | Total entries in the trace tree this turn. |
| `X-Ozma-Trace-Cost-Usd` | Total cost across the tree. |
| `X-Ozma-Trace-Latency-Ms` | Wall-clock latency across the tree (max of parallel paths). |
| `X-Ozma-Trace-Side-Effects` | Summary: `ci_minutes=N, external_calls=N, tokens=N`. |
| `X-Ozma-Trace-Outcomes` | Breakdown by outcome: `ok=9, deny=1, escalate=1, ...`. |
| `X-Ozma-Trace-Entries-By-Kind` | Count per kind: `dispatch=4;message=3;emit=2;gate=3`. |
| `X-Ozma-Trace-Backend` | Trace backend id (OpenTelemetry endpoint, Honeycomb dataset, etc.). |

Per-subsystem detail headers (e.g.,
`X-Ozma-Dispatches-By-Strategy`, `X-Ozma-Messages-Sent-Count`,
`X-Ozma-Motion-Safety-Violations`, `X-Ozma-Human-Gate-Count`)
remain as convenience views; they're all derivable from the trace
tree and can be omitted in minimal deployments.

### Integration with GATEs

Every gate evaluation produces a trace entry with
`kind=gate`, `subkind=<gate-kind>`, `outcome=<decision>`,
`decision_reason`, `approver?`. Compliance reporting queries the
trace backend with filters like
`kind=gate AND outcome IN (deny, modify, escalate)`.

### Integration with GDPR / HIPAA / SOX / FINRA reporting

Deployments configure a `trace-backend` Adapter (via the unified
Adapter primitive) that can be a compliance-friendly store
(append-only, cryptographically signed, retention-policy enforced).
The `*_layer` fields provide the five-layer audit content regulators
now require in 2026 (per California SB 243 / AB 489, FINRA 2026
oversight report, NIST AI Agent Standards Initiative).

### Secrets and Trace Redaction (normative)

Secrets — API keys, bearer tokens, passwords, signing keys,
service-account credentials — appear throughout adapter configs,
emit bodies, gate policies, and occasionally in message bodies.
v5.9.1 makes the contract explicit:

1. **Canonical reference form.** Secrets are referenced as
   `{@secret:NAME}` through the unified §REF syntax. No other
   form is valid; inline literal secrets in model output or
   adapter YAML trigger `secret_literal_in_spec` warnings.

2. **Resolution.** `{@secret:*}` refs resolve through the
   `Adapter kind=secret-store` registered for the deployment
   (env-vars, HashiCorp Vault, AWS Secrets Manager, GCP Secret
   Manager, Azure Key Vault). Exactly one secret-store adapter
   is active at a time; the shim routes all `{@secret:*}` refs
   through it.

3. **Redaction contract (normative).** When a secret ref is
   resolved, the resolved *value* is:
   - **Never** written to the trace tree — only the ref name
     and a `sha256(value)[:8]` fingerprint are stored.
   - **Never** written to telemetry, logs, or response headers.
   - **Never** returned in a trace backend query — backends
     receive pre-redacted entries.
   - **Never** emitted in `[protocol:describe-response]`.
   - **Auto-masked** in any grammar production that would
     otherwise surface it (e.g., `X-Ozma-Adapter-Config-Debug`
     replaces the value with `<redacted:sha256-fingerprint>`).

4. **Compliance tag.** Any trace entry whose resolution chain
   touched a `{@secret:*}` ref automatically carries
   `contains-secret: true` in its compliance_tags. Downstream
   trace-backend adapters MAY route these to more-restricted
   storage tiers.

5. **Telemetry visibility.** What *is* visible: `secrets_
   resolved_count` per turn, the ref names requested, the hash
   fingerprints, the secret-store adapter id. Enough for audit
   without ever exposing the value.

6. **Error surfaces.** `secret_unresolved` (name not in store)
   and `secret_adapter_unavailable` (no kind=secret-store
   registered) return to the caller without leaking whether
   the name exists in some other deployment — always respond
   identically regardless of root cause.

This is the first place in the spec where a grammar construct
(`{@secret:*}`) carries normative *non-observability* guarantees
about its contents. Trace-backend adapters MUST enforce the
redaction contract independently (defense in depth).

---

## Composition Patterns

Canonical combinations of the protocol's primitives that recur
across deployments. Naming them here is a documentation and
training-data convergence tool — not new protocol surface.

### Dev / Test / Review flow

Replaces hand-rolled verification pipelines. One PLAN with four
dispatches:

```
plan:
  step s1 dev    [dispatch target=ozma-coder-2 strategy=cheapest-capable]
  step s2 test   [dispatch strategy=self]                    # existing test suite
  step s3 review [dispatch strategy=verify n=5 confidence_floor=0.9]
  step s4 fix    [dispatch target=ozma-coder-2 strategy=cascade escalate_to=ozma-wizard-1]
                  precondition: { "confidence_below": 0.9, ref: "{@dispatch:s3}" }
```

Pattern unlocks: *"checking a hypothesis against N models"* as a
single integrated command rather than hand-rolled orchestration.

### N-Model Consensus

For high-stakes decisions, use `strategy=verify` (hypothesis) or
`strategy=vote` (pick one of options) across a diverse model pool:

```
[dispatch strategy=verify n=20 targets_policy=diverse-capable
          confidence_floor=0.95]
  hypothesis: "Refactored query returns identical rows to original for all inputs satisfying idx_user_tenant"
[/dispatch]
```

ε^N error reduction when errors are uncorrelated (diverse targets).
Use for migrations, schema changes, irreversible deletions.

### Mini-Project Promotion

Ad-hoc work grows up through durability tiers:

```
(turn 1) task-list          → implicit, session-scoped
(turn 4) [work-unit:promote from=tl:... to=mini-project]
(turn 12, when work warrants it)
         [work-unit:promote from=mp:... to=project]
           adapter: linear
           priority: P2
```

Pattern: don't over-spec at the start; let structure emerge as
work justifies it. Promotion is one-way (tier ↑ only).

### Handoff Chain with Unblock Cascade

Work flows between agents; completion unblocks downstream:

```
# agent A completes work and hands off
[message type=handoff to=agent:B refs=[{@workunit:X}]]
  completion_state: "ready for integration"
  requires_ack: true

# agent B acks (required before proceeding)
[message type=ack in_reply_to=m:1 accepted=true]

# when agent B completes the downstream work
[work-unit-edge from=linear:Z to=linear:Y kind=blocked-by]  # exists
# shim auto-walks the graph on status=done:
#   → emits [message type=unblock to=<owner of linear:Z>]
#   → Z.status auto-advances from blocked → todo
```

Entire cascade is declarative; no orchestrator required.

### Critical Section via Claim + Request

Distributed mutual exclusion over shared scope:

```
# agent A claims before working
[message type=claim from=agent:A]
  scope: [ file: src/core/auth.ts ]
  expires: "+4h"

# agent B wants to work on same file; asks first
[message type=request from=agent:B to=agent:A purpose=general
         refs=[{@message:claim-id}]]
  asking_for: "release of claim on src/core/auth.ts (I need to fix CVE-2026-001)"

# agent A decides, releases if appropriate
[message type=release in_reply_to=claim-id reason="agent B's fix has priority"]
```

Claims are advisory (not locks), so this pattern relies on agent
politeness. Pair with a Gate kind=policy if enforcement needed.

### Trust-Building Gates

Repeated similar decisions grow auto-approval over time:

```
[gate kind=human approver_rules={rule: named, id: matt}
      rising_threshold: 5
      cool_down_seconds: 15
      batch_similar: true
      explain_on_deny: required]
```

After 5 incident-free approvals of structurally identical gates,
the 6th auto-passes with telemetry flag `auto_approved_by_prior`.
Any denial resets the counter. Addresses the 93%-approval fatigue
problem without removing oversight.

### Payment-Gated Emit (v5.10 preview)

For agent-initiated transactions:

```
[emit id=e:1 type=trade-order idempotency_key=tr-2026-04-20-0001]
  broker: interactive-brokers, action: buy, symbol: NVDA, quantity: 100

[gate on=emit:e:1 kind=composite semantics=required]
  chain:
    - { kind: policy,   engine: opa,  policy_url: "trade-limits.rego" }
    - { kind: algorithm, endpoint: "https://risk/eval", threshold_max: 0.75 }
    - { kind: rules,    predicate: { "time_between": ["market_open", "market_close"] } }
    - { kind: payment,  escrow: ap2:account-123 }
    - { kind: informational, log_to: sox-audit }
    - { kind: human,    approver_rules: {rule: from_team, team: trading-desk}, only_if: { ">": ["{@emit:e:1}.amount_usd", 10000] } }
```

Composable gate chain provides compliance + risk + market hours +
value-transfer + audit + human oversight — in one declarative
block, all emitting trace entries.

### Cross-Agent Verification

Peer agents verify each other's claims:

```
# agent A makes a claim
[claim §6: auth refresh handler preserves all user sessions under network partition]

# agent A dispatches verification to a peer
[message type=request from=agent:A to=agent:verifier
         purpose=review refs=[{@claim:c6}]]
  asking_for: "independent review of the partition-survival claim"

# agent:verifier responds
[message type=response in_reply_to=<id>
         verdict=changes-requested]
  body: "Found edge case: client cookie TTL < session TTL breaks assumption. See line 42."
```

Composes COORDINATION peer messaging with CLAIM verification.

### Trial-Run Before Commit (dry-run)

Preview an expensive or risky action:

```
(set header) X-Ozma-Dry-Run: simulate

[emit type=email to="all-customers@example.com" ...]

(shim response, no email sent)
[trace-entry kind=emit dry_run=true outcome=would_approve
             gates_evaluated=[...] skipped_side_effects=1]
```

Dry-run validates all gates and computes cost/latency estimates
without executing. Useful for: deployment validation, gate-policy
tuning, user confirmation flows ("here's what would happen if you
proceed").

---

## Telemetry Schema

Every request emits a structured event for training feedback loop:

```json
{
  "session_id": "sess_abc123",
  "model_id": "qwen3.6-ozma-l3-v1",
  "level": 3,
  "turn": 1,
  "ack_observed": true,
  "ack_hmac_valid": true,
  "caps_acked": ["OUTLINE", "EDIT", "RATIONALE", "HINT", "WIRE"],
  "caps_disabled": [],
  "wire_active": true,
  "positions_addressed": ["§6", "§8"],
  "rationales_emitted": ["§6", "§8"],
  "rationale_unreferenced": [],
  "rationale_missing": [],
  "outline_valid": null,
  "hint_valid": null,
  "check_passed": true,
  "check_scope_errors": [],
  "check_regression_errors": [],
  "alias_undeclared": [],
  "wire_table_version": "7890",
  "wire_table_match": true,
  "prompt_tokens_external": 1240,
  "prompt_tokens_compressed": 380,
  "prompt_tokens_from_cache": 860,
  "completion_tokens_external": 210,
  "completion_tokens_compressed": 85,
  "session_cache_hits": 3,
  "session_cache_misses": 1,
  "session_cache_chunks_live": 4,
  "claims_emitted": ["§6", "§6"],
  "claims_verified": ["§6 returns {} when file is empty"],
  "claims_unverified": ["§6 never raises on malformed JSON"],
  "reasoning_emitted": true,
  "reasoning_branches_accepted": 1,
  "reasoning_branches_rejected": 1,
  "plan_emitted": false,
  "plan_steps_completed": null,
  "test_spec_emitted": ["§6"],
  "handoff_emitted": false,
  "transaction_count": 0,
  "confidence_scores": {"§6": 0.9, "§8": 0.4},
  "pattern_compression_active": false,
  "latency_ms": 1420,
  "tokenizer": "qwen",
  "streaming": true,
  "upstream_prompt_cache_tokens": 860,
  "hardware_class": "h100",
  "persona": "engineer",
  "memory_injected": 2,
  "memory_stored": 1,
  "memory_recalled": 0,
  "persona_change_requested": false,
  "persona_change_granted": null,
  "memory_guardrail_count": 0,
  "filter_version": "v1",
  "attn_positions_expanded": 0,
  "attn_tokens_added": 0,
  "bpe_aliases_active": 0,
  "embed_aliases_created": 0,
  "docstub_active": false,
  "structured_conf_used": false,
  "formal_claims_emitted": [],
  "formal_verify_status": {},
  "polyglot_entries": 0,
  "collab_weights": {},
  "federated_positions": 0,
  "dp_epsilon": null,
  "dp_delta": null,
  "dp_composition": null,
  "model_switch_cap_delta": [],
  "formal_verify_advisory_aspects_excluded": 0,
  "strict_antispoof_active": false,
  "silent_ack_ms": null,
  "math_blocks_emitted": 0,
  "math_refs_used": 0,
  "math_input_format": "unicode",
  "shape_annotations_emitted": [],
  "shape_verify_status": {},
  "shape_violations": [],
  "schema_edits_emitted": 0,
  "schema_compat_mismatches": [],
  "schema_breaking_count": 0,
  "dep_ops_emitted": 0,
  "dep_cve_notes": [],
  "effects_emitted": [],
  "effects_verify_status": {},
  "effects_pure_count": 0,
  "prompt_blocks_emitted": [],
  "prompt_injection_issues": [],
  "prompt_token_budget": {},
  "agent_state_goal": null,
  "agent_state_progress": null,
  "agent_state_progress_delta": null,
  "agent_state_scope": "workspace",
  "agent_delta_empty_count": 0,
  "agent_goal_complete": false,
  "dep_cve_unresolved_count": 0,
  "schema_format_illegal_op_count": 0,
  "effects_contradiction_count": 0,
  "prompt_token_budget_exceeded_count": 0,
  "caps_trained": ["OUTLINE", "EDIT", "RATIONALE", "CLAIM", "MEMORY", "UPGRADE"],
  "caps_accepted": ["MATH", "TENSOR-SHAPE", "SCHEMA", "EFFECTS"],
  "caps_emitted_as_trained": ["EDIT", "RATIONALE"],
  "caps_emitted_as_prompt_taught": ["SCHEMA"],
  "teaching_prompt_tokens_total": 420,
  "teaching_prompt_format_valid": {"SCHEMA": true, "MATH": null},
  "teaching_fallback_exhausted_caps": [],
  "upgrade_cap_acked": true,
  "style_active": "conversational",
  "style_overridden_count": 0,
  "theme_active": null,
  "theme_render_target": "markdown",
  "render_target_client": "markdown",
  "emoji_density_adjusted": false,
  "prosody_emitted": false,
  "prosody_tts_target": "plain",
  "prosody_attrs_stripped_count": 0,
  "voice_active": null,
  "voice_engine_resolution": null,
  "voice_fallback_count": 0,
  "persona_age": null,
  "persona_context_style_applied": null,
  "avatar_active": null,
  "avatar_render_target": "none",
  "avatar_engine_resolution": null,
  "motion_mode": "live",
  "motion_devices_declared": [],
  "motion_commands_emitted": 0,
  "motion_commands_dispatched": 0,
  "motion_safety_violations": 0,
  "motion_confirm_required_count": 0,
  "motion_confirm_granted_count": 0,
  "motion_confirm_denied_count": 0,
  "motion_confirm_timeout_count": 0,
  "motion_transport_errors": 0,
  "motion_emergency_stop_fired_count": 0,
  "motion_transport_latency_ms_p50": null,
  "motion_transport_latency_ms_p99": null,
  "motion_devices_by_transport": {},
  "vision_sources_declared": [],
  "vision_frames_in": 0,
  "vision_tier_requested": null,
  "vision_tier_used": null,
  "vision_format_used": null,
  "vision_bytes_delivered": 0,
  "vision_pipelines_invoked": [],
  "vision_pipeline_latency_ms_p95": null,
  "vision_annotate_primitives": {},
  "vision_annotate_mode": null,
  "vision_edit_ops": 0,
  "vision_edit_transactions": 0,
  "vision_edit_rollbacks": 0,
  "listen_sources_declared": [],
  "listen_utterances": 0,
  "listen_asr_conf_mean": null,
  "listen_prosody_emitted": false,
  "listen_emotion_labels_emitted": 0,
  "listen_sound_events_emitted": 0,
  "listen_voice_fingerprint_denied_count": 0,
  "sensor_sources_declared": [],
  "sensor_emits": 0,
  "sensor_privacy_location": "coarse",
  "sensor_privacy_violations": 0,
  "cap_probe_emitted": 0,
  "cap_probe_accepted": 0,
  "cap_probe_declined": 0,
  "cap_probe_timeout": 0,
  "cap_probe_upgrade_accepted": 0,
  "cap_probe_round_trip_ms_p95": null,
  "shim_profile_id": "shim-prod-us-east-1",
  "shim_profile_version": "1.3.0",
  "effective_caps": [],
  "shim_transforms_applied": [],
  "shim_transforms_count": 0,
  "shim_transform_latency_ms": 0,
  "shim_transform_cost_usd": 0.0,
  "shim_transform_budget_exceeded": false,
  "shim_transform_failures": [],
  "client_tier": "C2",
  "client_adapter": "ozma-native",
  "model_tier": 3,
  "model_adapter": "ozma-native",
  "mode": "default",
  "quadrant": "C2xL3",
  "taught_caps": [],
  "teaching_tokens": 0,
  "workunit_tier": "mini-project",
  "workunit_id": "mp:a4c299",
  "workunit_touched": ["mp:a4c299"],
  "workunit_status_after_turn": "in-progress",
  "workunit_promoted_this_turn": null,
  "workunit_decay_imminent": false,
  "pm_adapter": null,
  "pm_adapter_writes_attempted": 0,
  "pm_adapter_writes_succeeded": 0,
  "routing_context_mode_requested": "coordinates",
  "routing_context_mode_used": "coordinates",
  "routing_context_ttl_seconds": 600,
  "routing_context_freshness_ms": 34200,
  "routing_context_refresh_count": 0,
  "routing_query_count": 0,
  "routing_query_empty_count": 0,
  "user_preferences": {
    "cost_vs_quality": 0.35,
    "urgency": 0.8,
    "risk_tolerance": 0.2,
    "verification_rigor": 0.7,
    "enrichment_level": "auto"
  },
  "ledger_monthly_budget_remaining_usd": 4.20,
  "deadline_hours_remaining": 52.4,
  "compliance_tags_active": ["us-only", "soc2"],
  "compliance_filter_hits": 3,
  "dispatch_trace_id": "t:7fa1d8e2c4b93601",
  "dispatches_emitted": 5,
  "dispatches_honored": 5,
  "dispatches_cancelled": 0,
  "dispatches_by_strategy": {
    "cheapest-capable": 2,
    "cascade": 1,
    "verify": 1,
    "self": 1
  },
  "dispatch_depth_max": 2,
  "dispatch_tree_size": 7,
  "rollup_cost_usd": 0.0421,
  "rollup_latency_ms": 8420,
  "rollup_side_effects": {
    "ci_minutes": 0,
    "external_calls": 3,
    "tokens": 42019
  },
  "rollup_ledger_entries": 5,
  "rollup_ledger_overflow": 0,
  "dispatch_classifier_vs_model_cost_delta_usd": -0.018,
  "dispatch_classifier_vs_model_latency_delta_ms": -1240,
  "strategy_quality": {
    "race_realized_vs_expected_latency_ratio": null,
    "vote_disagreement_rate": null,
    "blend_diversity_score": null,
    "ensemble_diversity_score": null,
    "cascade_escalation_rate": 1.0,
    "cascade_escalation_success_rate": 1.0,
    "verify_agreement_mean": 0.98,
    "verify_agreement_variance": 0.004
  },
  "human_gate_count": 0,
  "human_gate_auto_approved_count": 0,
  "human_gate_denied_count": 0,
  "human_gate_timeout_count": 0,
  "human_gate_cool_down_violations": 0,
  "provenance_emitted": true,
  "provenance_spans_count": 3,
  "provenance_lost_count": 0,
  "context_enrichment_level_effective": 0.65,
  "task_categories": [
    { "tag": "code-generation/refactor", "confidence": 0.91 },
    { "tag": "python",                    "confidence": 0.88 },
    { "tag": "billing/pricing-logic",     "confidence": 0.72 }
  ],
  "agent_id": "agent:linear:MOB-487",
  "agent_scope": "workspace",
  "agent_wake": "passive",
  "coordination_transport": "a2a",
  "coordination_wire": "ozma-compressed",
  "coordination_teaching_applied": "pre-taught",
  "coordination_teaching_reverted": false,
  "messages_processed_count": 3,
  "messages_sent_count": 2,
  "messages_delivered_count": 2,
  "messages_by_type": {
    "handoff": 1,
    "status": 1,
    "unblock": 1
  },
  "handoffs_emitted": 1,
  "handoffs_accepted": 1,
  "handoffs_declined": 0,
  "handoff_depth_used": 1,
  "oscillation_detected": false,
  "blocker_edges_added": 0,
  "blocker_edges_resolved": 1,
  "blocker_unblocks_emitted": 2,
  "claims_acquired": 0,
  "claims_released": 0,
  "claims_active_at_turn_end": 1,
  "claims_expired": 0,
  "claim_conflicts_surfaced": 0,
  "channels_subscribed": 2,
  "channel_posts": 0,
  "subscriptions_active": 5,
  "subscription_overflow_count": 0,
  "inbox_unread_count": 2,
  "inbox_shown_count": 2,
  "inbox_hidden_count": 0,
  "inbox_fetch_requests": 1,
  "inbox_fetch_hits": 1,
  "message_rate_limit_queued": 0,
  "message_compliance_blocked": 0,
  "message_dedup_collisions": 1,
  "peer_identities_seen": ["agent:linear:MOB-488", "agent:platform-reviewer"],
  "session_registry_mode": "summary",
  "session_registry_entries": 3,
  "session_refs_resolved": 2,
  "session_refs_by_form": {
    "whole":     1,
    "by_label":  1,
    "turn":      0,
    "tagged":    0,
    "anaphora":  0
  },
  "session_refs_tokens_fetched": 1240,
  "session_search_queries": 1,
  "session_search_results_returned": 3,
  "session_nl_hint_fired": true,
  "session_refs_denied": 0,
  "session_labels_assigned_this_turn": 0,
  "session_shares_granted_this_turn": 0,
  "session_sensitive_blocks": 0
}
```

`null` fields indicate the capability was not exercised this turn.
`persona_change_requested` is true when the model emitted `persona: X` this
turn; `persona_change_granted` is true/false when that request was evaluated,
null when no request was made. `tokenizer` is the tokenizer family declared in
the model registry — used for cross-deployment token cost comparisons.
`upstream_prompt_cache_tokens` counts tokens that hit the upstream provider's
prompt cache (separate from the shim's own session cache). `hardware_class`
enables per-GPU latency analysis for LoRA retraining decisions.

---

## Capability Profiles

No capability is compulsory. Every capability is declared in the registry
and confirmed in the handshake ack — a model that omits a capability from
its ack simply doesn't get it, and the shim falls back to prose for that
type. Small models can implement any subset.

**Named profiles** (declared as `"profile"` in the registry, advisory):

| Profile | Minimum caps | Target model size | Use case |
|---------|-------------|-------------------|----------|
| `minimal` | EDIT | 0.5B–3B | Small models. The point in configuration space — edit scope, constrained decode, hmac — is carried in the registry entry per-model, not bundled here. See §Tunes and Model Capacity for the field set. |
| `standard` | EDIT, INTERACT, RATIONALE, OUTLINE, MEMORY | 7B–13B | Multi-turn agent, session memory |
| `full-v2` | All v1.x–v2.x caps | 30B+ | Full protocol, pattern compression, handoff |
| `research-v3` | All caps through v3.0 | 70B+ | STRUCTURED-CONF, FORMAL-CLAIM, POLYGLOT, COLLAB |
| `federated-v4` | All caps through v4.0 | 70B+ | FEDERATED, DP, GRAMMAR — multi-node training pipelines |
| `ml-code-v5` | `standard` + MATH, TENSOR-SHAPE, EFFECTS | 13B–30B | Tensor-shape correctness for PyTorch/JAX/TF; math-aware docstrings |
| `api-code-v5` | `standard` + SCHEMA, DEP, EFFECTS | 13B–30B | Microservices, schema evolution, dependency hygiene |
| `agent-v5` | `standard` + AGENT-STATE, PLAN, PROMPT | 13B–30B | Long-running autonomous agents; Component-application editing |
| `full-v5` | All caps through v5.0 | 70B+ | Everything — research and frontier deployments |
| `expressive-v5.4` | `standard` + STYLE, THEME, PROSODY, VOICE | 7B–13B | Conversational products; voice assistants; companion UX |
| `embodied-v5.5` | `expressive-v5.4` + AVATAR, MOTION | 13B–30B | VTubers, service robots, teleoperation, embodied companions |
| `perception-v5.7` | `standard` + VISION, LISTEN, SENSOR, CAP-PROBE | 13B–30B | Multimodal observation pipelines — co-pilot agents, surveillance, assistive tech |
| `multimodal-v5.7` | `embodied-v5.5` + `perception-v5.7` + VISION-ANNOTATE, VISION-EDIT | 30B+ | Full multimodal in/out — co-pilot editing, embodied perception-action loops |
| `full-v5.7` | All caps through v5.7 | 70B+ | Everything — frontier deployments across all modalities and both directions of negotiation |
| `orchestration-v5.8` | `standard` + PLAN, ROUTING-CONTEXT, DISPATCH, WORK-UNIT, AGENT-STATE | 13B–30B | Model-directed dispatch; planner-led routing across a model pool; mini-project execution |
| `pm-native-v5.8` | `orchestration-v5.8` + full WORK-UNIT tiers (task-list/mini-project/project) + PM adapters | 30B+ | First-class project integration (Linear / Jira / GitHub / MCP-passthrough) |
| `verify-v5.8` | `standard` + DISPATCH (with `verify` strategy) + FORMAL-CLAIM | 7B–30B | High-confidence verification workflows; hypothesis-check pools |
| `full-v5.8` | All caps through v5.8 | 70B+ | Everything — frontier deployments with planner-led dispatch and first-class PM integration |
| `specialist-dispatch-v5.10` | `orchestration-v5.8` + `perception-v5.7` + SPECIALIST-DISPATCH + VISION-DISPATCH + LISTEN-DISPATCH + SENSOR-DISPATCH | 13B–30B | Reasoning model that routes perception subtasks to specialist models; model-directed multimodal analysis |
| `full-v5.10` | All caps through v5.10 | 70B+ | Everything |
| `interleaved-v5.11` | `standard` + REASONING + REASONING-PERSIST + THINK-ACT + THINKING-MODE + AGENT-STATE | 7B–30B | Native multi-step tool-call loops with preserved reasoning; matches K2.6 / OpenAI / Anthropic wire shape while keeping Ozma's compression. AGENT-STATE is required because REASONING-PERSIST keys its cache by the AGENT-STATE `goal:` hash. |
| `autonomous-v5.11` | `orchestration-v5.8` + `interleaved-v5.11` + SCHEDULE + TOOL-RESULT-CACHE | 13B–30B | Proactive 24/7 agents — scheduled wake-up, tool-result-aware cache, reasoning continuity across fires. The protocol's complete answer to the K2.6 "300-sub-agent / 4000-step / background-execution" agenda. |
| `full-v5.11` | All caps through v5.11 | 70B+ | Everything — frontier deployments with native interleaved tool-use, preserved reasoning, and first-class scheduled wake-up. |
| `authority-civilian-v5.13` | `embodied-v5.5` + Gate (kind=`authority`; `attestation_type` restricted to `regulatory_license \| operator_approval \| contract_binding`) | 13B–30B | Civilian dual-use actuation — regulated delivery drones, medical delivery above threshold, financial wire transfer above threshold, regulated-utility actuation. `comms_handshake` attestation explicitly excluded from this profile; defence-class posture requires `authority-defence-v5.13`. Registry enforces scope narrowing and `issuer_epoch` pinning. |
| `authority-defence-v5.13` | `authority-civilian-v5.13` + Gate (kind=`authority`; full `attestation_type` set including `comms_handshake`) + mandatory `authority_chain` (minimum two links: regulator → operator) + mandatory `revocation_check: live-pull` (no cached TTL permitted) | 13B–30B | Defence-class dual-use actuation — aircraft fire-class actuators, weapon-system release, kinetic actuation above human-safety threshold. Deployment must pin a specific IHL/LOAC policy bundle and pass NATO responsible-use-of-AI-in-defence operator certification. Not available on public deployments; registry flags `defence: true` with explicit operator opt-in. |
| `full-v5.13` | All caps through v5.13 (`full-v5.11` + Gate kind=`authority`; excludes `authority-defence-v5.13` defence-class unless explicitly opted in) | 70B+ | Everything through v5.13 — adds authority-gated-autonomy Gate kind for civilian dual-use substrates. Defence-class posture is opt-in, not default. |
| `rich-emit-v5.14` | `expressive-v5.4` + DRAW (authored subshapes — svg, ascii, glyph-ref, math, map, music, chemistry, table, layout; ref form enabled for graph/tree/claim/outline/reasoning; raster subshape disabled; mermaid/graphviz/plantuml available as render backends per shim config, not as author targets) + GRAPH + TREE + PERSONA.`emoji_pack` + PERSONA.`visual_idiom` | 7B–13B | Conversational products that need rich visual emit — diagrams, glyph-packed messages, aligned tables — without the cost/risk/latency of bound raster models. Default configuration for most v5.14 deployments. |
| `multimodal-draw-v5.14` | `multimodal-v5.7` + `rich-emit-v5.14` + DRAW (raster subshape enabled via SPECIALIST-DISPATCH) + VISION-ANNOTATE round-trip with DRAW emit | 30B+ | Full bidirectional visual conversation — user annotates / sketches → agent reads → agent replies with DRAW in matching idiom → user annotates on top. Requires a registered raster specialist (image model) with `raster_emit: enabled`; inherits composite Gate (policy + kill-switch-check + informational). |
| `full-v5.14` | All caps through v5.14 | 70B+ | Everything — frontier deployments with authority-gated-autonomy (civilian default; defence opt-in), bidirectional visual conversation (raster opt-in), and the completed sense/emit symmetry matrix across text, motion, audio, and visual. |
| `long-running-v5.15` | `standard` + TASK | 7B–13B | Any agent that spawns subprocesses or long-running tool calls — installs, builds, test runs, dev servers, training jobs. Elastic-await keeps fast commands synchronous; `wake_on` predicates replace polling; ambient surfacing prevents forgotten tasks from going silent. Minimum useful TASK posture. |
| `autonomous-v5.15` | `autonomous-v5.11` + TASK (cross-session publication enabled; `task-store` adapter configured for workspace scope) | 13B–30B | Proactive 24/7 agents whose work survives session boundaries. Scheduled fires and dispatches are TASK kinds with shared budget/overrun/telemetry; running tasks are visible to any session in the workspace. The v5.15 superset of `autonomous-v5.11`. |
| `full-v5.15` | All caps through v5.15 | 70B+ | Everything — frontier deployments with first-class long-running task visibility, cross-session publication, and the ambient surfacing cadence that makes forgotten background work observable by default. |

A model may declare fewer caps than its profile suggests — the profile is
a human-readable hint for operators, not a constraint on the registry.

**Profile-composition note.** Profiles that chain other profiles
(`autonomous-v5.11` = `orchestration-v5.8` + `interleaved-v5.11` + …)
inherit their bases' capability sets transitively but **do not inherit
version locks**. A future edit that removes a cap from
`orchestration-v5.8` does not automatically remove it from
`autonomous-v5.11`. Treat the chained references as cap-set shorthand at
the time the profile was declared, not as a live link. When evolving a
base profile, explicitly visit every downstream profile and decide
whether the change propagates. The `full-v5.N` profiles are the only
ones that mean "all caps through vN.x" in a live sense; all
`<theme>-v5.N` profiles are frozen at declaration.

**What the ack declares:** The model's ack line lists only the capability
fingerprints it was trained against. A `minimal`-profile model acks two
caps; a `full`-profile model acks twenty. The shim enables exactly the
acked set from the registry-declared set — the intersection.

```python
MODEL_CAPS = {
    "qwen1.5b-ozma-minimal": {
        "level": 2,
        "profile": "minimal",
        "caps": {"EDIT": "b2c1d3"},
    },
    "mistral7b-ozma-standard": {
        "level": 3,
        "profile": "standard",
        "caps": {
            "EDIT": "b2c1d3", "INTERACT": "d8f3e2",
            "RATIONALE": "e1a7f4", "OUTLINE": "a4f9c1", "MEMORY": "cd34ef",
        },
    },
}
```

**Adding caps incrementally:** A model retrained with one new capability
adds exactly one entry to its `caps` dict, acks the new fingerprint, and
gains that capability. No other changes required. This is the primary
upgrade path for iterative LoRA training.

---

## Wire Compression

Wire compression is a family of **corpus-derived substitution maps** the shim
applies transparently to reduce token count. The protocol grammar never sees
compressed symbols; expansion happens before parsing and after emission.

**Substitution maps.** A map is a lookup table from frequent tokens to short
aliases, generated from corpus statistics. The technique works on any repetitive
wire surface — code keywords, SQL schemas, config YAML, telemetry JSON, natural
language prose. The map is deployment-specific: which keywords are frequent,
which symbols are cheap in the target tokenizer, which substitutions don't
collide with structural tokens.

**Map generation (shim-side).** The shim generates maps from corpus analysis:
1. Collect token-frequency statistics from the deployment's code/files/logs.
2. Select high-frequency tokens that compress well in the target tokenizer.
3. Assign short aliases (single characters, digraphs, or learned BPE tokens).
4. Verify that substitutions are AST-safe (don't substitute inside strings or
   comments unless explicitly allowed).
5. Publish the map fingerprint for handshake negotiation.

This is the same technique as L2 Stage-1 dialect design, but at lower ambition:
no model training, just a substitution table.

**Map negotiation.** The shim advertises its active map via the `map:<hash>`
field in the handshake probe. The model acks `map:<hash>` if it recognises the
map (trained or taught). On mismatch, both sides fall back to the reference
encoding (protocol.md§Reference Encoding). Map misses log
`map_fingerprint_mismatch` and continue uncompressed.

**Map types:**

| Type | What it maps | Typical savings | Use case |
|------|-------------|-----------------|----------|
| Keyword substitution | `function` → `ƒ`, `return` → `→` | 30–60% | Code compression |
| Identifier aliasing | `UserAuthenticationService` → `b1` | 40–70% | Repo-specific identifiers |
| Vocabulary substitution | `therefore` → `∴`, `because` → `∵` | 10–20% | Prose compression (WIRE) |
| Session legend | `long_name_1` → `a1` | 50–80% | Per-session identifiers |

**Dynamic maps.** The shim can generate maps on demand for a specific workload
(e.g., "compress this SQL dump") or load cached maps for known formats (Google
Docs schema, FHIR Observation, etc.). A map-literate model (protocol.md§Capability
Levels) accepts any map it recognises the fingerprint for; new formats require
only a new map schema + render backend, not model retraining.

**Composability.** Multiple maps compose layerwise: keyword substitution runs
first, then identifier aliasing, then session legend. Each layer's output is the
next layer's input. The shim reverses the stack on expansion.

**Shim behaviour:**
- Generate maps from corpus analysis on session start or on cache miss.
- Cache maps per-workspace and per-deployment.
- Apply maps on the way to the model, reverse on the way back.
- Validate that expanded output round-trips to the original (lossless check).
- Log compression ratio per map layer in telemetry.

**Protocol:** see protocol.md§Handshake for `map:<hash>` negotiation; see
protocol.md§Reference Encoding for the fallback encoding grammar.
