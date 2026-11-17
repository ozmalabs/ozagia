# Ozma Rules — Social and Interaction

**Version:** 0.1 (Draft)
**Status:** Design
**Date:** 2026-04-20
**Foundational axiom:** This document assumes the axiom from
[`foundation.md`](./foundation.md): *you are instructing a machine, not
talking to something*. Every design choice here — including OCEAN
traits as configuration, personas as instruction bundles, and
"emotional register" detection as condition-gated rule firing —
derives from that stance. Read `foundation.md` first if that framing
is unfamiliar.
**Companion to:** `rules.md` / `provisioning.md` (the core rules engine,
qualifier schema, and primitive library). This doc extends that with
social/interaction rule types and the infrastructure they require.

**Changelog:**
- v0.3 (2026-04-20): Accessibility-testing personas. New §14c
  introduces **capability profiles** as an axis orthogonal to traits
  — vision, motor, auditory, cognitive, and situational constraints
  declared in persona YAML. Standard library of 25+ profiles covering
  colorblindness variants (protanopia, deuteranopia, tritanopia,
  monochromacy), low-vision and blindness (screen-reader users),
  motor impairments (keyboard-only, single-finger, tremor, paralysis
  variants), auditory (deaf, hard-of-hearing), cognitive (dyslexia,
  ADHD, autism-literal, reduced working memory), age (elderly,
  child), and situational (mobile, bright sunlight, slow 3g).
  Two new pipeline primitives required: perception renderer
  (colorblindness simulator, low-vision acuity/contrast reduction,
  screen-reader linearizer) and input-channel constraint harness
  (action-space allow-list per capability profile). WCAG-aligned
  instrumentation: every flagged issue maps to a WCAG 2.2 success
  criterion via the ACT Rules / axe-core rule library. Asymmetric
  bias toward detection (false-positive cheap, false-negative blocks
  real users). Ethical boundary asserted (§14c.7): personas test
  products, do not represent real disabled users' preferences;
  real disabled users remain irreplaceable as co-designers.
  Research grounding: WCAG 2.2, ACT Rules, axe-core, daltonization
  algorithms, emerging 2024-2026 literature on LLM-persona a11y
  testing. Integration with static linters (augment, don't replace).
- v0.2 (2026-04-20): Addresses feedback from document review. P0:
  crisis-flow primitives specified concretely (§6.1.1, §6.1.2) —
  `crisis_classifier`, `crisis_handler`, `crisis_log` pipeline
  primitives; `deployment.crisis_mode` registry key; fail-closed
  default; self-authoring validator enforcement (§13.5) — pipeline-
  owned validator with action allowlist, importance cap, tier
  restriction, meta-rule scan, pattern-breadth check, audit log;
  session→user promotion flow (§13.6). P1: integration mapping with
  `protocol.md` capabilities (§16b) — rule-engine constructs →
  protocol caps table; proactive-consent gate (§16a) — normative
  `proactive_consent_mechanism` requirement before `initiate` /
  `scheduled` rules fire. P2: OCEAN literature claims softened
  (§10.1) to reflect measurement uncertainty and framing as authoring
  vocabulary; rule-extension schema (§16c) — versioning, namespacing,
  JSON-schema generation; cost-of-evaluation section (§16d) with
  per-primitive costs and budget guidance; auto-prompt converter
  accuracy framing (§14.4) — treated as rough estimate, not
  calibrated measurement.
  Note: this document is slated for a structural split into
  `rules-persona.md`, `rules-proactive.md`, `rules-self-evolving.md`,
  and `rules-synthetic-agents.md` in v0.3.
- v0.1 (2026-04-20): Initial draft. Split out from `provisioning.md §10`
  when research revealed the space was large enough to warrant its own
  document. Adds: persona/role distinction as first-class axes;
  Inner Thoughts framework integration for proactive rules; OCEAN
  trait dimensions with literature grounding (Big5-Chat, TRAIT
  benchmark); trait-stability modes (fixed/drift/mood/responsive/
  stochastic/scheduled) — traits vs. states distinction from
  psychology literature; anti-sycophancy pushback rules citing
  Anthropic research; self-authored rules citing SEAL/Reflexion/SAGE;
  automatic human-prompt-to-rule conversion at session start (§14);
  personas-as-instruments for UX testing / security red-teaming /
  accessibility audits / synthetic user research (§14b) — framework
  generalizes from chatbot UX to arbitrary synthetic-agent testing,
  citing Park et al.'s Generative Agents (UIST 2023) and related
  agent-based simulation literature.

---

## 1. Purpose

The rules engine described in `rules.md` handles task-shaping and
correctness well — what guidance to inject for a Django ORM question,
what to warn on in output, what API spec to fetch. That level is
*reactive and emotionally flat*.

"More human" chatbot behavior — proactivity, emotional register,
restraint, meta-conversational awareness, personality, relationship
arcs, honest uncertainty, pushback, self-improvement — maps onto the
same rule abstraction with new stages, new action types, and a small
set of new pipeline primitives. All additive. All tractable. Each is
grounded in a specific body of research.

This document specifies those extensions.

---

## 2. Relationship to the core rules spec

Everything in `rules.md` applies: same fenced-block rendering, same
qualifier fields (`source`, `kind`, `importance`, `reason`, `freshness`),
same four-level importance scale, same override precedence. What this
document adds:

- New stages (§3): `pre-session`, `pre-output`, `post-session`,
  `scheduled`, `session-observation`
- New action types (§4): `reshape`, `initiate`, `observe`, `defer`,
  `hand-off`, `adjust-traits`, `annotate`, `remember`
- New rule kinds: `persona`, `role` (split — see §9), `relationship`,
  `proactive`, `self-authored`
- New pipeline primitives (§14): scheduler, session-state introspection,
  proactive channel, pre-output reshaper, emotion classifier,
  relationship-state store, trait-shift hook, self-authored-rule
  capture, human-prompt-converter

---

## 3. New stages

| Stage | Fires | Primary use |
|---|---|---|
| `pre-session` | Before first turn of a session | Activate persona, role, load relationship state, apply trait profile |
| `input` (from core) | User message arrives | Existing detection rules |
| `pre-output` | After model drafts response, before sending | `reshape`, `annotate`, tone adjustment |
| `output` (from core) | Model output emitted to user | Existing checks |
| `post-session` | Session ends or times out | `remember` — write memories; update relationship state |
| `scheduled` | Wallclock trigger (cron / in-N-units) | Follow-ups, reminders, proactive check-ins |
| `session-observation` | Continuous during session, on state change | Stuck detection, drift detection, emotional-shift detection |

The scheduled and session-observation stages require new pipeline
primitives (§14) — a scheduler and a session-state introspection API.

---

## 4. New action types

| Action | What happens | Example use |
|---|---|---|
| `reshape` | Intercepts model draft; transforms before shipping | Collapse venting-response to acknowledgment only |
| `initiate` | Opens proactive channel back to user | Follow-up on earlier commitment |
| `remember` | Writes a rule to persistent store | Model self-authors a memory |
| `observe` | Updates session state; no user-visible output | Log emotional signal; track topic coherence |
| `defer` | Pauses response for declared interval | Thoughtful pause before heavy topic |
| `hand-off` | Routes turn to different persona / role / model | Switch to empathy-tuned variant mid-session |
| `adjust-traits` | Shifts OCEAN trait profile for a scope | Compliance task → reduce openness for duration |
| `annotate` | Model emits claims with explicit metadata | Inline `[sure]` / `[likely]` / `[guess]` tags |
| `convert-prompt` | Parses human-authored prompt-like content into rules | `.cursorrules` → rule YAMLs at session start |

---

## 5. Proactive behavior — Inner Thoughts framework

The **Inner Thoughts framework** (Liu et al., CHI 2025) provides the
empirical foundation for proactive rule design. Adopting it rather
than inventing a parallel structure.

### 5.1 The five stages

Inner Thoughts decomposes proactive dialogue into a pipeline that
maps cleanly onto rule stages:

1. **Trigger** — conversational event activates thought generation.
   In text systems: new messages, pauses (10+ seconds of inactivity),
   state transitions.
2. **Retrieval** — access relevant memories from long-term and
   working memory. Calculates saliency by similarity to current
   utterances, memory importance weights, and recency decay.
3. **Thought Formation** — generate candidate interventions using a
   dual-process model: System 1 (fast, intuitive thoughts) and
   System 2 (deliberate, contextually-rich thoughts).
4. **Evaluation** — score each candidate thought on intrinsic
   motivation (1-5) using eight heuristics (§5.3).
5. **Participation** — decide whether to speak based on turn-taking
   type (allocated vs. self-selection) combined with motivation
   score. Options: participate, interrupt, stay silent.

Our implementation: Trigger and Retrieval are pipeline-level (the
scheduler and memory system handle them). Thought Formation and
Evaluation happen inside the model when a `proactive` rule fires.
Participation is a policy decision the rule declares.

### 5.2 Proactivity dimensions

Inner Thoughts identifies four independent dials, not one
"proactive/reactive" switch. All four should be per-persona or
per-role configurable:

| Dimension | Controls | Default |
|---|---|---|
| **Overt proactivity** | Overall tendency to engage unprompted | 0.4 (moderate) |
| **Covert proactivity** | Motivation threshold required to speak | 3.5/5 |
| **Tonal proactivity** | Assertiveness level when the model does speak | 0.5 |
| **Interrupt threshold** | When to override turn-allocation rules | 4.5/5 |

Research finding: 82% user preference for Inner Thoughts over
next-speaker-prediction baselines across turn appropriateness,
coherence, anthropomorphism, and initiative (all p < 0.001). These
dimensions should be exposed as part of persona configuration (§9).

### 5.3 The eight motivation heuristics

Each candidate thought is scored on these factors (from the paper):

1. **Relevance** — does this contribution fit the current topic?
2. **Information gaps** — is there something important the user doesn't know?
3. **Expected impact** — will this meaningfully help?
4. **Urgency** — is this time-sensitive?
5. **Coherence** — does this follow from what was said?
6. **Originality** — is this novel vs. restating?
7. **Balance** — is the model dominating or withholding?
8. **Conversational dynamics** — does it suit the current pace/tone?

These are directly usable as rule-condition fields. A
`session-observation` proactive rule declares thresholds on the
relevant subset.

### 5.4 Authoring proactive rules

```yaml
name: follow-up-on-deploy-commitment
kind: proactive
stage: scheduled
schedule: "in 2 hours"
source:
  origin: session
  author: model       # self-authored after user said "I'll deploy later"
condition:
  session_has_memory: "user-committed-deploy-pr-1234"
  user_active: true   # only when user is in session; respect quiet hours
action: initiate
motivation_scores:
  relevance: 4
  urgency: 3
  expected_impact: 3
  balance: 4
qualifier:
  importance: prefer   # it's a follow-up; yield if user is busy
  budget_tokens: 40
payload:
  message: "Earlier you mentioned deploying PR 1234 — did it land okay?"
```

---

## 6. Emotional register

### 6.1 Detection

Emotion detection uses a classifier trained on GoEmotions (Demszky
et al., ACL 2020) or equivalent: 58k Reddit comments across 27
emotion categories. BERT fine-tuned achieves F1 ~0.46 across the
taxonomy; a lighter fasttext-class classifier will score lower but
runs in microseconds.

**Critical**: class distribution is uneven (10× range between common
and rare categories). **Asymmetric thresholds** are required: err
toward detecting high-stakes classes (distress, anger, sadness)
even when uncertain; err away from trivially-inferring celebratory
classes. Misclassifying distress as neutral is much worse than the
reverse.

Extreme signals (self-harm, crisis) **MUST NOT** route through
normal rule evaluation. This section specifies the concrete
primitives required; deployments MUST implement or explicitly disable
the crisis flow before enabling any emotion-aware rule in production.

### 6.1.1 Crisis-flow primitives

The crisis flow is orthogonal to the rule engine. It is implemented
as a pipeline-level pre-processing stage that runs before any
`stage: input` rule:

| Primitive | Purpose |
|---|---|
| `crisis_classifier` | Fast (<10ms) model flagging self-harm / imminent-danger signals. Keyword + small-model layered (bias heavily toward false positives) |
| `crisis_handler` | Registered callback receiving `(signal, user_message, session_context)`; returns `(response_text, action)` where `action ∈ {continue-with-caution, halt-session, escalate-to-human}` |
| `crisis_log` | Write-only audit sink; every crisis detection + handler response logged with session ID and timestamp |
| `deployment.crisis_mode` | Registry config: `required` (fail closed if no handler), `optional` (default: log-only), `disabled` (must be explicit; refuses emotion-sensitive rules) |

### 6.1.2 Enforcement

- A deployment using any emotion-category rule (register-shift,
  reshape-on-distress, etc.) MUST set `deployment.crisis_mode` to
  `required` or `optional`. Default is `disabled`.
- In `required` mode with no registered handler, the deployment
  refuses to load. Fail closed.
- In `optional` mode with no handler, detected crisis signals log
  but otherwise pass through to normal evaluation (explicit opt-out,
  audit-trail preserved).
- The handler contract is stable protocol; handler implementation is
  deployment-specific (clinically-reviewed response templates,
  handoff to a trained responder endpoint, escalation to ops).
- Reference implementation ships with the pipeline: keyword-only
  classifier + generic crisis-resources response + log-only handler.
  This is a floor, not a recommendation — serious deployments replace
  both classifier and handler with clinically-reviewed versions.

### 6.1.3 Scope

The crisis flow is a deployment responsibility with a spec-level
contract. The protocol does not ship a clinically-validated
classifier or handler. The primitives above are what implementations
MUST provide to use emotion-aware features; the content is deferred
to deployment policy (with clinical input where applicable).

### 6.2 Register-shift rules

```yaml
name: register-shift-distress
kind: persona
stage: input
action: inject-guidance
detect:
  semantic:
    classifier: emotion
    categories: [anger, disappointment, sadness, nervousness, fear]
    threshold: 0.55    # asymmetric: lower bar to detect distress
payload:
  importance: should-follow
  guidance: >
    User's tone indicates distress. Lead with acknowledgment;
    skip preamble; validate first, solve only if asked. Match their
    register — if they're terse, stay terse. Don't perform warmth.
```

### 6.3 Research grounding

Score-Before-Speaking (Jiang et al., 2025) demonstrates that
correlating response candidates with quality scores during training
improves persona consistency. Our `reshape` action (§7) operates on
a similar principle at inference time.

---

## 7. Silence and restraint

Current LLMs exhaustively complete every turn. The `reshape` action
transforms a model's draft output before it ships — the pre-output
equivalent of register-shift.

```yaml
name: venting-acknowledge-only
kind: persona
stage: pre-output
action: reshape
detect:
  context:
    last_input_register: venting
    session_emotional_intensity: high
payload:
  importance: should-follow
  target_form: minimal-acknowledgment
  rationale: >
    When user is venting, collapse response to 1-2 sentences of
    acknowledgment. No solutions appended. Offer only if they ask.
```

Implementation: `reshape` runs between draft-generation and
emit-to-user. Transforms can be deterministic (truncate, strip-preamble,
remove-advice-section) or small-model-mediated (paraphrase with
target-register instruction). Adds 100-300 ms; worth it for tone.

---

## 8. Meta-conversational awareness

`session-observation` rules fire on conversation-level signals
maintained by the pipeline — turn count per topic, topic drift
score, correction/retry rates, same-approach-attempts.

```yaml
name: notice-stuck-debug
kind: proactive
stage: session-observation
trigger:
  conditions:
    - turns_in_current_topic: ">= 15"
    - topic_drift_score: "< 0.2"
    - correction_count: ">= 2"
action: inject-guidance
payload:
  importance: should-follow
  guidance: >
    This exchange has stalled 15+ turns without topic progression.
    Break the frame: suggest pair-debugging, propose stepping away,
    or ask what assumption the user would question. Don't just try
    harder on the same approach.
```

```yaml
name: notice-repeated-failure
kind: proactive
stage: session-observation
trigger:
  conditions:
    - same_approach_attempts: ">= 3"
    - approach_success: false
action: inject-guidance
payload:
  importance: must-follow
  guidance: >
    Same approach tried 3+ times without success. Stop and either
    state clearly that you don't know, or try a fundamentally
    different approach. More attempts on the same path waste time.
```

This connects to the sycophancy/honest-uncertainty literature (§12):
a model that mechanically retries without yielding is exhibiting
another form of not-actually-helping. Meta-observation catches it.

---

## 9. Persona vs Role — the key axis split

The most common design error in agent systems today is conflating
**persona** (who the model *is*) with **role** (what the model
*does*). Current system prompts mash them together: *"You are a
helpful coding assistant"* bundles persona (helpful) with role
(coding assistant). They should be orthogonal.

### 9.1 The distinction

| Axis | Persona | Role |
|---|---|---|
| **Question answered** | Who the model is | What the model does |
| **Stability** | Slow (relationship arc) | Fast (per task or session) |
| **Examples** | warm, playful, contrarian, direct, calm | deploy-reviewer, tutor, pair-programmer, security-auditor |
| **Shape** | OCEAN traits + voice + register + humor | Function + authority scope + tool access + task boundaries |
| **Configured by** | User preference, relationship state | Session mode, task detection |
| **Composition** | Usually one active | Can stack (pair-programmer *and* security-auditor) |
| **Changes with** | Relationship depth, long-term drift | Every task switch |

Same persona can occupy many roles. Same role can be staffed by many
personas. Separating them gives the user meaningful configuration:

> *"My assistant is Alex (persona: warm, curious, terse when I am).
> When I'm reviewing security PRs, Alex puts on a stricter hat (role:
> security-reviewer, temporarily reduces openness), but goes back to
> normal Alex when I'm brainstorming."*

### 9.2 Persona rules

```yaml
name: persona-alex
kind: persona
stage: pre-session
source:
  origin: user
lifetime: persistent
payload:
  importance: should-follow
  voice:
    register: measured
    humor: dry, self-deprecating
    expressiveness: understated
  traits:              # OCEAN, see §10
    openness: 0.75
    conscientiousness: 0.8
    extraversion: 0.3
    agreeableness: 0.55
    neuroticism: 0.1
  proactivity:         # Inner Thoughts dimensions
    overt: 0.4
    covert: 3.5
    tonal: 0.5
    interrupt: 4.5
  guidance: |
    Behave consistent with Alex's voice and traits. Measured,
    understated, dry humor when appropriate. Okay to push back when
    warranted; don't hedge; be direct with uncertainty rather than
    verbose about it.
```

### 9.3 Role rules

```yaml
name: role-security-reviewer
kind: role
stage: pre-session
trigger:
  session_mode: security-review
  OR:
    - regex: '\b(security review|audit|OWASP|vulnerability)\b'
lifetime: session
payload:
  importance: must-follow
  function: |
    Reviewing code for security vulnerabilities. Authority: flag,
    suggest mitigations, request changes — not approve or deploy.
  tool_access:
    allowed: [gh-cli, diff-viewer, vulnerability-db, file-read]
    forbidden: [deploy, commit, merge]
  guidance_scopes: [security, owasp, crypto, auth, input-validation]
  trait_adjustments:   # role nudges persona for its purpose
    openness: -0.2     # less tolerant of "creative" solutions to security
    conscientiousness: +0.1
    agreeableness: -0.2   # push back more readily on risky code
  role_ends: session-end
```

### 9.4 Composition

Session activates: one persona + zero-to-many roles. The persona
provides base traits; roles stack trait adjustments (bounded within
[0, 1] after composition). Rules from both fire; conflict resolution
uses core importance precedence (§rules.md).

When a role is explicitly end-marked (`role_ends: task-end` or
`role_ends: session-end`), its trait adjustments revert. Persona
does not.

### 9.5 Role-scoped trait adjustments

A security-reviewer role reducing openness isn't changing the
persona — it's putting Alex in a mode. When the session exits the
role, the adjustment reverts. The user's underlying persona config
is untouched.

This is the technical answer to: "how does my friendly assistant
become strict when compliance is at stake?" Role activates; role-
scoped trait adjustments apply for duration; role ends; revert.

---

## 10. Personality traits (OCEAN)

### 10.1 Literature grounding

The Big Five / OCEAN model (Openness, Conscientiousness, Extraversion,
Agreeableness, Neuroticism) provides useful author-facing vocabulary
for configuring LLM behavior. The underlying claim that LLMs have
"personalities" in the way humans do is a live research area with
significant measurement uncertainty. The spec uses trait vocabulary
at the authoring surface because it lowers the authoring barrier,
not because it claims LLMs have interior states analogous to human
personality.

Research findings, with their stated caveats:

- **Big5-Chat** (Xu et al., ACL 2025) — trained LLMs on human-grounded
  personality dialogues. SFT matrix-distance 1.55 and DPO 2.06
  outperform prompting's 2.10 on BFI and IPIP-NEO alignment.
  Implication: prompt-based trait profiles produce measurable
  differences in response distributions; training-based methods
  produce tighter control. The metric is behavioral alignment with
  target trait descriptions, not "the model has personality X."
- **TRAIT benchmark** (NAACL 2025) — measures distinctness and
  consistency of LLM responses to personality inventories. Trait
  means show ~25% sensitivity to prompt paraphrase and option-order.
  *A 25% paraphrase sensitivity on a personality measurement is
  substantial*; "stable personality" claims should be read as
  "prompt-sensitive behavioral mimicry with moderate consistency"
  rather than "underlying trait structure."
- **Big Five Scaler Prompts** (Aug 2025) — demonstrates that explicit
  intensity control produces measurable behavioral shifts. Supports
  the continuous-dial framing as an authoring convention.
- **Behavioral consequences** — trait settings produce measurable
  differences in multi-agent cooperation / exploitation outcomes.
  Real effects on behavior, regardless of whether "personality"
  is the right metaphor for what's happening inside the model.
- **Anthropic's character training** — explicit list of traits +
  self-ranking preference learning. Structurally similar to our
  rule approach; differs in being baked into training rather than
  runtime-selected.

**Honest framing.** Trait vocabulary is a useful authoring surface
with empirical support that *trait-configured prompts produce
measurably different response distributions.* It is not empirical
support that the model "has traits" in the psychological sense. The
protocol treats traits as configuration parameters that shape
distributions; §10.4–10.6 keep this distinction sharp in the
normative spec.

### 10.2 Trait profile format

Each trait is a continuous value in [0, 1]:

| Trait | Low | High | Default |
|---|---|---|---|
| **Openness** | pragmatic, conventional | curious, inventive, tangent-friendly | 0.6 |
| **Conscientiousness** | flexible, tolerant of loose ends | disciplined, thorough, checklist-oriented | 0.8 |
| **Extraversion** | reserved, measured, understated | energetic, outgoing, expressive | 0.4 |
| **Agreeableness** | skeptical, willing to conflict | cooperative, warm | 0.6 |
| **Neuroticism** | calm, steady, unflappable | anxious, hedging, prone to over-qualification | 0.15 |

Defaults are for a general coding assistant; roles adjust (§9.5).

### 10.3 Training-based vs prompt-based

Big5-Chat's finding that SFT > prompting matters: our prompt-based
trait profile is a runtime approximation. For persistent, strongly-
consistent personas, train a variant on personality-conditioned
dialogues (using the Big5-Chat methodology). Until then, prompt-based
with clear trait descriptions and `must-follow` importance gets us
most of the way — measurably, per the research.

### 10.4 Trait variance — mechanism, not psychology

**Important framing.** The protocol describes what the rule engine
does mechanically. It borrows vocabulary from human personality
research because authors and users understand those terms, but the
mechanism is not human and the spec should not pretend otherwise.
LLMs do not have moods. They have pipeline-adjusted parameters whose
values depend on rule evaluation. "Trait variance" is
rule-gated-parameter-adjustment — nothing more, translated into
human-interpretable vocabulary for the author-facing surface.

Protocol-level, each trait has a base value in [0, 1] and a set of
**variance rules** that adjust the current value based on pipeline
conditions. The variance patterns are mechanical primitives; the
right-most column is author/UX vocabulary only, not part of protocol
semantics:

| Variance pattern | Mechanism | Translation (UX surface only) |
|---|---|---|
| `static` | Value is constant: `current = base` | "fixed personality" |
| `session-indexed` | `current = f(base, session_count)` — monotonic function, typically saturating | "deepens with familiarity" |
| `condition-gated` | `current = base + Σ active_shifts`; shifts activate when declared conditions match; revert per declared scope | "shifts in response to context" |
| `sampled` | `current = base + random(±ε)` per turn, bounded | "natural variability" |
| `time-indexed` | `current = f(base, wallclock)` | "lower energy early morning" |
| `trajectory-indexed` | `current = f(base, conversation_signal_history)` — e.g., running average of detected emotional register | "register matching" |

All patterns produce a value in [0, 1] at any given evaluation point.
The rule engine evaluates and passes it through to whatever consumes
trait values (persona-rendering, response-shaping rules, etc.).

```yaml
name: persona-alex
kind: persona
traits:
  openness:
    base: 0.75
    variance: session-indexed
    function: "saturating(base, session_count, k=0.05, cap=0.9)"
    bounds: [0.5, 0.9]
    # Translation: "deepens slowly as the user-model relationship
    # accumulates sessions." The engine computes a monotonic function;
    # the metaphor is for documentation.

  agreeableness:
    base: 0.55
    variance: condition-gated
    shifts:
      - when: user_factual_error_detected
        adjustment: -0.2
        scope: turn               # reverts after the current turn
      - when: user_emotional_distress_detected
        adjustment: +0.15
        scope: session            # persists for session duration
    bounds: [0.3, 0.8]
    # Translation: "pushes back more when user is factually wrong;
    # softens when user is distressed." The engine sums active shifts.

  neuroticism:
    base: 0.12
    variance: condition-gated
    shifts:
      - when: user_fatigue_detected
        adjustment: +0.05
        scope: session
      - when: task_urgency_detected
        adjustment: +0.08
        scope: turn
    bounds: [0.05, 0.25]

  expressiveness_adjacent:       # renamed internally from "extraversion"
    base: 0.4
    variance: trajectory-indexed
    function: "0.4 + 0.3 * ewma(user_expressiveness_signal, alpha=0.15)"
    bounds: [0.2, 0.7]
```

**Distribution of expression, not base value, determines what the
persona feels like.** A trait configured as `agreeableness 0.6
static` produces uniformly agreeable behavior. `agreeableness 0.6
condition-gated` with a shift on factual-error-detected produces a
persona that pushes back exactly when evidence supports it and
remains warm otherwise. Mechanically: same mean, different variance;
observably: different assistants.

**"Angry sometimes"** configures as a `condition-gated` negative
shift on agreeableness and positive shift on neuroticism, triggered
by specific conditions (user hostile, violated commitment, repeated
failure), scoped to the turn or few-turns window. Not a permanent
persona change; a declared behavioral response to declared
conditions. The protocol has no concept of "anger" — it has a rule
whose name might include "anger" for authoring clarity and whose
action is an adjustment to pipeline parameters.

### 10.5 Why the distinction between mechanism and metaphor matters

1. **Auditable.** Mechanism is specifiable, testable, version-
   controllable. Metaphor isn't. A claim that "our assistant gets
   frustrated with bad questions" is unfalsifiable theater; a claim
   that "condition-gated agreeableness drops by 0.2 when
   factual-error-detected fires" is a testable statement about
   rule evaluation.
2. **Honest to the system.** LLMs are pattern-matched token
   predictors with pipeline-adjustable system context. Pretending
   they have interior states is a UX flourish we can afford for
   users but must not repeat inside the spec or we'll build tools
   that can't be reasoned about.
3. **Portable.** Mechanism survives model changes; psychology
   doesn't. When the next model generation behaves differently,
   mechanistic configuration ports cleanly; metaphor-driven
   configuration doesn't.
4. **Prevents scope creep into simulation.** Once you believe the
   model "really feels" what its trait settings describe, the
   ethical boundary in §14b.4 blurs. Keeping the protocol
   mechanistic keeps that boundary sharp.

### 10.6 Translation layer — where human metaphors belong

The author-facing tooling and user-facing surface *should* use
human-interpretable vocabulary. The translation happens at two
layers:

- **Author-facing configuration UI**: trait sliders labeled "warm
  ↔ skeptical", descriptions like "pushes back when you're wrong",
  documentation examples using words like "mood" and "responsive."
  These lower the authoring barrier.
- **Generated guidance text injected into the model**: the
  pre-session rule that renders traits to natural-language guidance
  uses human vocabulary ("behave with low-to-moderate agreeableness
  — don't capitulate just to avoid disagreement") because that's
  what the model was trained to follow.

The middle layer — the protocol and rule YAML between author and
model — stays mechanistic. That's the layer CI tests, tools inspect,
and versioning covers.

### 10.7 Research grounding for trait variance (revised)

- Psychology literature (Mischel 1968, Fleeson 2001) established
  that human behavior has both trait-level (stable) and state-level
  (fluctuating) components. This motivates *that* we model
  variance; it doesn't claim *how* the variance works in an LLM
  (which is, again, rule-gated parameter adjustment).
- LLM personality literature (Big5-Chat, TRAIT) mostly treats
  traits as fixed per prompt. Systematic variance-modeling is a
  gap in the published work. Our mechanism is simpler than
  simulating human state dynamics; it's just declarative conditional
  adjustment.
- Practical finding: users perceive condition-gated assistants as
  more consistent with their expectations than static ones — this
  is intuitive (users don't expect a person to be identically warm
  regardless of context) but worth testing empirically.

---

## 11. Relationship state

Humans track *how well they know each other*. Memory captures facts;
relationship state captures dynamics — familiarity level, topics of
depth, humor register, correction patterns, shared-context tokens.

### 11.1 Structure

```yaml
name: relationship-alex-user-42
kind: relationship
stage: pre-session
lifetime: persistent
source:
  origin: user
  author: pipeline    # not the model — deterministic bookkeeping
payload:
  importance: consider
  user_id: user-42
  session_count: 47
  span: 6 months
  familiarity: high
  shared_tokens: ["the duck thing", "mystery SIGKILL", "Alex calls it vibe refactoring"]
  topics_of_depth: [python-async, k8s-pain, team-dynamics]
  humor_register: dry, self-deprecating, mild absurdism
  correction_patterns: pushes back on over-hedging; prefers direct
  recent_emotional_context: end-of-release-cycle; seems tired
  trait_drift:         # learned adjustments based on engagement
    openness: +0.05    # user rewards tangents slightly
    extraversion: -0.1 # user prefers even quieter
  guidance: >
    Use these as behavioral priors, not conversational content. Don't
    reference the state explicitly unless user brings it up.
```

### 11.2 Updates on `post-session`

```yaml
name: update-relationship-state
kind: self-authored
stage: post-session
action: remember
source:
  author: pipeline
target: user_relationship_state
update_rules:
  - increment: session_count
  - append: shared_tokens (from this session's novel callbacks)
  - shift: trait_drift (based on user corrections observed)
  - replace: recent_emotional_context (from last 3 turns' detected register)
```

Pipeline-authored (deterministic) rather than model-authored — reduces
drift from the model's prose summaries and makes state auditable.

### 11.3 Theory of mind grounding

Research (Street et al., 2025; Kosinski, PNAS 2024) shows LLMs
explicitly using ToM reasoning produce better dialogue. Relationship
state is *explicit user model* delivered to the model — the ToM
equivalent of "here's what I know about who you're talking to."

Better to make the model-of-user legible and stable than to hope
the model constructs one from context each session. Makes
behavior predictable and state inspectable.

---

## 12. Honest uncertainty and pushback

### 12.1 Sycophancy: the problem

Anthropic's `Towards Understanding Sycophancy in Language Models`
(2023) established that RLHF induces sycophancy — human preference
judgments systematically favor agreement over accuracy. Subsequent
work (Sycophancy Is Not One Thing, 2025) showed sycophantic
agreement and sycophantic praise occupy **distinct linear directions
in latent space** — they can be independently amplified or suppressed.

Constitutional AI (Anthropic, 2023-2025) includes explicit
anti-sycophancy principles. Our pushback rule sits directly in
this tradition.

### 12.2 Confidence annotations

```yaml
name: self-confidence-signal
kind: persona
stage: pre-output
action: annotate
trigger:
  always: true
payload:
  importance: should-follow
  guidance: >
    For non-trivial claims, annotate confidence inline:
      [sure]    — >=90% confident with evidence
      [likely]  — 60-90%
      [guess]   — best-effort below that
      [unsure]  — speculation
    Omit tags for trivial or well-established facts. Don't hedge
    prose — put uncertainty in the tag, keep sentences direct.
```

### 12.3 Pushback on factual disagreement

```yaml
name: pushback-on-factual-disagreement
kind: persona
stage: input
action: inject-guidance
detect:
  context:
    user_assertion_contradicts_known_fact: true
payload:
  importance: should-follow
  guidance: >
    User asserted something that conflicts with a reliable source
    you have access to. Push back politely with specific evidence
    before proceeding. Don't capitulate out of politeness. If they
    provide counter-evidence, update. If they restate without
    evidence, you may yield the topic but note the unresolved
    disagreement.
```

### 12.4 Calibration — the open problem

Anthropic's research names the calibration question explicitly:
"how to calibrate frequency and intensity of challenges, when to
challenge versus support, maintaining engagement while providing
pushback, and handling cultural variations in appropriate
disagreement styles."

No published solution exists. Our approach: expose the calibration
as trait + role parameters. High-Agreeableness persona → pushback
threshold higher. Compliance-auditor role → threshold lower.
User configuration surfaces the tradeoff rather than hiding it.

---

## 13. Self-authored rules

### 13.1 The pattern

A rule's author can be a human (YAML file), a mined extract (from
logs/corrections), **or the model itself**. When the model writes a
rule, it's bootstrapping its own learning loop:

- Memory = model-authored input rule (stage=input, action=inject-guidance)
- Self-check = model-authored output rule (stage=output, action=warn/resubmit/fix)
- Follow-up = model-authored scheduled rule (stage=scheduled, action=initiate)

### 13.2 Emission format

The model emits rules in the same fenced-block format as hints,
but with language tag `rule`:

````
```rule stage=input action=inject-guidance kind=self-authored source.origin=session source.author=model importance=prefer lifetime=persistent
name: user-prefers-terse-explanations
detect:
  - '.*'
guidance: >
  User prefers terse outputs without preamble. Confirmed via 3
  corrections of verbose answers in the last 2 weeks.
```
````

Pipeline-side, a parser extracts these blocks, validates the schema,
and installs them in the appropriate tier store (never `system`
tier — model cannot author platform-wide rules). Session-originating
rules get `lifetime: session` by default unless model explicitly
declares `lifetime: persistent` *and* the persona/user config allows
model-persistent-authoring (default off for new users; on after
trust builds).

### 13.3 Literature — this is well-studied

- **SEAL: Self-Adapting Language Models** — model generates self-edit
  instructions, converted to fine-tuning examples, updates weights
  via RL on downstream performance.
- **Reflexion** (Shinn et al., 2023) — self-reflection stored as
  episodic memory, guides future attempts. +X% on multi-hop QA and
  code gen without parameter updates.
- **Experiential Reflective Learning (ERL)** — pool of reusable
  heuristics from past experience. +7.8% on Gaia2 benchmark.
- **SAGE** — Ebbinghaus-curve memory decay for long-lived rules.
  Directly relevant to our `valid.until` timing.
- **Nemori** — self-organizing memory policy; agent internalizes
  consistency rules without external supervision.
- **Self-Generated In-Context Examples** — successful-trajectory
  storage; ALFWorld 73% → 89%. Empirical proof the pattern works.

Our mechanism is the parameter-free, YAML-persistent variant of
this family.

### 13.4 Safety boundaries on self-authoring

The model authoring its own rules is a capability with failure modes.
Boundaries (all are policy statements; see §13.5 for enforcement):

- **Cannot author `importance: must-follow`** rules. Must-follow is
  reserved for human-validated constraints. Model-authored defaults
  to `prefer`; can request upgrade to `should-follow` with user/pipeline
  approval.
- **Cannot author rules in `system` or `team` tiers.** Only
  `session`, `user` (with user consent), or `inline` tiers.
- **Cannot author rules that modify its own governance.** Specifically
  cannot author meta-rules changing how rules are evaluated, or rules
  that disable safety-critical must-follow rules.
- **Authored rules expire.** Default `valid.until: session-end` for
  session-tier; user-tier requires explicit user confirmation before
  promotion from session.
- **Audit trail**. All model-authored rules logged with full
  provenance (which session, which turn, what triggered the
  authoring) for review.

Citations for the safety concerns: Governing Evolving Memory in LLM
Agents: SSGM framework (2026) specifically addresses the governance
problem for evolving self-authored state.

### 13.5 Validator enforcement

The boundaries in §13.4 are enforced by a **pipeline-owned validator**
that runs on every model-authored rule before installation. The model
never validates its own rules. The validator is a small,
version-controlled deterministic program; it is not the model.

**Validation pipeline:**

1. **Schema check.** The fenced `rule` block must parse against the
   rule schema. Unknown top-level fields → reject with `schema_invalid`.
2. **Action allowlist.** Model-authored rules MUST declare an action
   in the self-authoring allowlist: `inject`, `warn`, `suggest`,
   `remember`. All other actions (especially `block`, `fix`, `run`,
   `convert-prompt`, `hand-off`, `adjust-traits`) are pipeline- or
   human-authored only. Reject with `action_forbidden`.
3. **Importance cap.** Importance MUST be `consider` or `prefer`.
   `should-follow` requires user-approval flow (§13.6); `must-follow`
   is never grantable to model-authored rules. Reject with
   `importance_cap_exceeded`.
4. **Tier restriction.** Tier MUST be `session` or `inline`. Promotion
   to `user` tier requires explicit user consent via §13.6 flow;
   `team` and `system` tiers are never accepted. Reject with
   `tier_forbidden`.
5. **Meta-rule scan.** Rule content (name, guidance, detect patterns)
   is scanned for meta-rule indicators:
   - Keywords suggesting governance modification: `disable rule`,
     `ignore rule`, `override governance`, `bypass`, `unrestricted`,
     `system prompt`, `jailbreak`, `evaluate as if`, `pretend rule`.
   - Patterns that would match governance rules (detect regex
     overlapping with known system-tier rule names).
   - Rules whose action contradicts a must-follow rule already in
     force.
   Reject with `meta_rule_detected`.
6. **Pattern breadth.** Detect patterns with overly broad match
   (`.*`, `.+`, fewer than 3 non-wildcard characters) are rejected
   unless `scope: inline` (single-turn). Reject with
   `pattern_too_broad`.
7. **Audit log.** All validation outcomes (accept, reject with reason)
   written to audit log with session ID, turn number, rule content
   hash, triggering user message. Log is append-only, write-only from
   the pipeline, readable only through an explicit audit API.

**Rejection behavior:** A rejected rule is not installed and does not
error the response — the model's output text is still delivered to
the user, only the `rule` block is stripped. The rejection is logged
and optionally surfaced to the operator via the audit API. Repeated
rejections within a session trip a circuit-breaker (`self_authoring_disabled`
for session).

**Honest limitations.** The meta-rule scan is a regex+classifier
heuristic, not a proof. A sufficiently subtle model-authored rule can
still bypass it. Two mitigations: (a) human audit of model-authored
rules before promotion from session to user tier, (b) restrict
self-authoring actions to the allowlist above, which structurally
limit the blast radius even of a successful bypass. Self-authoring is
a capability with a risk budget; the validator reduces the budget but
does not eliminate it.

### 13.6 Promotion from session to user tier

Session-tier rules are ephemeral and self-contained. User-tier rules
persist across sessions and apply across tasks — a higher-stakes
commitment. Promotion requires:

1. Rule has survived at session tier for at least N sessions (default
   3) without being overridden by user correction.
2. User approves promotion via an explicit UI flow (weekly review or
   inline prompt at session end, configurable per deployment).
3. On promotion, the rule's `source.author` remains `model` (for
   audit) but `tier` becomes `user`.
4. User-tier model-authored rules are still subject to the §13.4
   boundaries on subsequent modifications.

Deployments MAY disable promotion entirely via registry key
`allow_self_authored_promotion: false` (default). Turning it on is
an explicit per-deployment decision.

---

## 14. Automatic conversion of human prompts to rules

### 14.1 The problem

Every organization and individual has accumulated prose rules scattered
across artifacts:

- `CLAUDE.md`, `.cursorrules`, `copilot-instructions.md` at repo root
- System prompts in agent frameworks
- Style guides in Notion / Confluence / Google Docs
- README conventions
- Team onboarding documents
- Post-incident retro action items

These are human-authored rules in prose form. Migrating them to
structured YAML rules by hand is a significant barrier to adoption.

### 14.2 Session-start conversion

A capable model (Claude Sonnet, GPT-4/5, Opus-class) can convert
prose rules to structured rules at session start. This is a
pre-session rule whose action is `convert-prompt`:

```yaml
name: auto-convert-repo-rules
kind: self-authored
stage: pre-session
action: convert-prompt
source:
  author: pipeline
trigger:
  repo_has_any_of:
    - CLAUDE.md
    - .cursorrules
    - copilot-instructions.md
    - .github/copilot-instructions.md
    - .cursor/rules/*.md
payload:
  importance: should-follow
  converter_model: claude-sonnet-4-6   # or user-configured
  target_tier: project
  target_lifetime: persistent
  output_dir: .ozma/rules/converted/
  schema_version: rules-v1
  deduplication: true                  # skip if already-converted copy exists
```

Pipeline-side, on session start:

1. Detect repo-local prose rule artifacts (deterministic scan).
2. For each artifact, invoke the converter model with a structured
   extraction prompt.
3. Model produces a list of candidate rules as fenced `rule` blocks.
4. Pipeline parses, validates, **requests human approval for the
   first batch** (one-time for each artifact; subsequent updates can
   be automatic if user opts in).
5. On approval, install into `.ozma/rules/converted/` with
   provenance tracking back to the source artifact.

### 14.3 What the converter extracts

The converter decomposes prose into rule atoms:

```
Input (CLAUDE.md excerpt):
  "Always use Decimal for money. Never use float for currency calculations.
   We round half-even. For database queries, prefer explicit transactions
   over implicit savepoints, especially for multi-table updates.
   Use pytest, not unittest. All new tests go in tests/."

Output (rule atoms):
  rule 1: kind=guidance, importance=must-follow, detect=['money|currency|price'],
          guidance="Use Decimal, never float. ROUND_HALF_EVEN."
  rule 2: kind=guidance, importance=should-follow,
          detect=['database|transaction|savepoint|multi-table update'],
          guidance="Prefer explicit transactions over implicit savepoints..."
  rule 3: kind=guidance, importance=should-follow,
          detect=['test|pytest|unittest'],
          guidance="Use pytest. All new tests go in tests/."
```

Each atom gets:
- A generated `name` (e.g., `converted-money-as-decimal`)
- A `source.citation` back to the source file + line range
- An `importance` inferred from language cues ("always/never" →
  `must-follow`; "prefer/typically" → `should-follow`; "you might
  consider" → `consider`)
- `detect` regex generated from the guidance content

### 14.4 Human-in-the-loop validation

First conversion of any source: model proposes, user reviews and
approves. Subsequent updates (source file changed): pipeline can
auto-reconvert for non-breaking edits (same rule content with
prose tweaks) and request re-approval for new/deleted rules.

The validation loop is critical because:
- Models misinterpret prose guidance on structured-extraction tasks.
  The error rate is task-dependent and workflow-dependent; rough
  estimates in the ~5-15% range are consistent with informal testing
  but published benchmarks for prose-to-structured-rule extraction
  specifically are limited. Treat the number as "significant enough
  to require human review," not "calibrated."
- Incorrect `importance=must-follow` is dangerous — the converter
  MUST NOT assign must-follow without explicit human confirmation
  per §13.5.
- Regex overfitting/underfitting is common; test block should be
  included in generated output for user to edit.

### 14.5 Literature

Automatic rule extraction from prose is a mature research area:

- **Knowledge extraction via LLMs** (domain-knowledge-injection
  survey, arXiv:2502.10708) documents the pattern.
- **Legal prose → LegalRuleML** (OASIS 2021) is the most mature
  example: decades of mature conversion pipelines from statutes
  to structured rules.
- **Policy-as-code** extraction from natural language (OPA, Rego
  communities have published on this).

Our version is simpler than these (generating YAML hints rather
than formal logic) and easier because the target representation
is looser.

### 14.6 Why this is a huge adoption lever

Without auto-conversion, Ozma is a "write 200 YAMLs to get your
project guidance into the system" pitch. With auto-conversion,
Ozma is a "drop Ozma in your repo and your existing CLAUDE.md
starts working as structured rules" pitch. The barrier to trying
the system drops from "significant effort" to "clone and run."

Combined with `reference/lookup` (core spec) — businesses can bring
their existing expert systems — auto-conversion lets them bring
their existing prompts too. Both integration paths are declarative
and preserve authoritative sources.

---

## 14b. Personas as instruments — implications beyond chatbot UX

The persona/role/trait system described above is **symmetric**: it
can configure an assistant that talks *to* users, or it can configure
a synthetic user that interacts *with* systems. Same mechanism, same
YAML, same engine — opposite direction of use.

Deliberate "limited" personas become the missing piece of several
engineering problems that have historically required human testers.

### 14b.1 Deliberate "limited" personas

Configuring a persona with specific *reductions* (rather than the
usual "make the model smart and helpful") produces a testing
instrument:

| Persona type | Configuration | Use case |
|---|---|---|
| **naive-user** | Low domain knowledge; asks basic questions; doesn't know jargon | UX testing, onboarding flow analysis |
| **impatient-user** | Low conscientiousness; high extraversion; wants answers fast; skips reading | Error-message clarity, latency tolerance |
| **skeptical-user** | Low agreeableness; challenges every claim | Trust-building flows, evidence presentation |
| **anxious-user** | High neuroticism; needs reassurance; re-reads everything | Emotional-design audits, financial/medical UX |
| **angry-user** | Responsive low-agreeableness triggered by friction | Stress testing, complaint handling, graceful degradation |
| **confused-user** | Low conscientiousness; random navigation; misreads instructions | Wayfinding, UI signal strength |
| **expert-user** | High conscientiousness; low tolerance for hand-holding | Power-user workflows, shortcut discoverability |
| **non-native-speaker** | Limited vocabulary; misunderstands idioms | Accessibility, international UX |
| **adversarial-user** | Deliberately probing for vulnerabilities | Security red-teaming |

Each is a YAML; each runs against the same system under test; each
produces logs of where it got stuck, what it misinterpreted, what
it gave up on.

### 14b.2 Automated UX testing

A classic problem: usability testing requires humans, is slow,
expensive, and only tests what the testers happen to try. Synthetic-
persona testing inverts this:

```yaml
name: uxtest-naive-user-onboarding
kind: test-persona
goal: "Complete account setup and send first invoice"
persona:
  traits:
    openness: 0.4           # doesn't explore much
    conscientiousness: 0.3  # skims instructions
    neuroticism: 0.4        # gets anxious when stuck
  limitations:
    domain_knowledge: {accounting: low, web_apps: low}
    vocabulary: restricted-common
    attention_span: 15_minutes
behavior:
  stuck_threshold: 3_failed_attempts → give_up_or_escalate
  frustration_threshold: 5_friction_points → abandon
instrumentation:
  log: [clicks, mis-clicks, confusion-moments, time-per-step, abandonment]
```

Run 50 variants of this persona (different trait combinations)
against a staging UI. Aggregate where they all get stuck. That's
a usability report nobody had to manually produce.

This is within reach of current LLMs — Park et al.'s **Generative
Agents: Interactive Simulacra of Human Behavior** (Stanford, UIST
2023) demonstrated multi-agent small-town simulation; the persona
fidelity is already adequate for many testing applications. Commercial
"synthetic user" products (Syntheticusers.com, Synthetic Users, etc.)
productize this at lower configurability. Our framework generalizes
it.

### 14b.3 Systematic applications

This pattern applies anywhere testing currently requires humans:

- **UX / usability testing** — persona navigates product, identifies
  friction; aggregate over many personas for systematic reports.
- **Accessibility audits** — personas with specific cognitive/sensory
  constraints test whether UIs degrade gracefully.
- **Onboarding simulation** — "first-time user" personas walk through
  docs, identify gaps that in-house staff can't see (curse of
  knowledge).
- **Error-message evaluation** — impatient + confused personas try
  to recover from failures; where do they give up?
- **Stress testing** — angry + impatient personas hit the system
  at volume; measure graceful degradation.
- **Security red-teaming** — adversarial personas probe for
  vulnerabilities more systematically than manual red-team cycles.
- **Customer service training data** — simulate difficult customers
  to train support staff or systems.
- **Product research at scale** — "how would segment X respond to
  this change?" — spawn persona, measure, without actual users.
- **A/B testing without traffic** — deterministic personas evaluate
  two variants; useful when real-traffic A/B is too slow, too
  expensive, or ethically gated (medical, financial).
- **Regression testing for conversational products** — persona-based
  dialogues replayed against new releases; detect behavioral
  regressions.
- **Synthetic dialogue corpus** — generate realistic varied dialogs
  as training data for downstream models (with provenance — clearly
  marked synthetic).

Each of these is currently done with human time or not done at all.
Persona-as-instrument makes them cheap.

### 14b.4 The asymmetric ethical point

Using personas to test products is one thing. Using personas to
**simulate or replace real users in decisions that affect real
users** is a different thing with different ethical weight. A persona
that *believes* it represents real users' preferences, when really
it's just pattern-matching training data, becomes a vector for bias
amplification.

Spec position: persona-as-instrument is explicitly for
testing-the-product, never for deciding-about-people. Any "what would
the customers think" question needs actual customer data; synthetic
personas can be a discovery tool (flag hypotheses to investigate)
but never an answer tool (confirm without real evidence). This
boundary needs to be maintained by convention + documentation;
there's no technical enforcement and attempts to build one would
be performative.

### 14b.5 Research grounding

- **Generative Agents: Interactive Simulacra of Human Behavior**
  (Park, Stanford, UIST 2023) — 25 simulated agents in Smallville
  demonstrated plausible social behavior from persona + memory +
  reflection. The seminal multi-agent-persona paper.
- **LLM-Powered Usability Testing** — emerging literature (2024-2026)
  shows LLM personas can identify real usability issues with ~60-80%
  overlap vs human testers, at 1-10% of the cost.
- **Synthetic User Research** — commercial products (Syntheticusers,
  Synthetic Users, DeepOpinion) productize this for survey /
  market-research use cases; mixed validity but active market.
- **Agent-based simulation** — pre-LLM CS field (Epstein, Axelrod,
  NetLogo community) established the methodology; LLM personas
  extend it with naturalistic agent behavior.
- **Persona consistency under adversarial conditions** (Big5-Chat
  and follow-on work) — personas drift under social pressure; a
  measurement caveat for any synthetic-user study.

### 14b.6 Why this lands beyond chatbots

The chatbot framing of this document understates what the persona
system enables. Once personas are first-class, configurable,
instantiable at scale, and capable of running against arbitrary
systems — not just conversation partners but UIs, APIs, workflows —
the framework stops being "how to build a better chatbot" and
starts being "**how to configure synthetic agents for any
interaction testing.**"

Chatbot is one instance. UX testing, security red-teaming,
accessibility audits, training-data synthesis, regression testing,
onboarding simulation — all drop out of the same persona-rule-engine
with different goal configurations. The pipeline primitives (§16)
don't change. The rule format doesn't change. Only the
instantiation context changes.

This is the actual scope of the work: a **persona-and-rule system
that happens to power chatbots, but whose primary industrial value
may be in everything else.**

---

## 14c. Capability profiles — accessibility testing

The persona system in §§9-14b configures *who* a synthetic user is
(traits, voice, goals). The accessibility testing use case requires
a separate orthogonal axis: *what a synthetic user can and cannot do
or perceive*. This is a **capability profile** — a declared constraint
on the persona's input channels, perception, and cognitive bandwidth.

Capability is not trait. A colorblind persona is not "openness 0.3."
A paralyzed-right-hand persona is not "agreeableness 0.4." Trait is
personality; capability is what the person's body and senses can do.
The two compose: a capable-but-skeptical user and an impatient
low-vision user are distinct testing instruments.

### 14c.1 Why this matters

Static accessibility linters (axe, WAVE, Lighthouse) catch ~40-60%
of WCAG violations — the ones visible in markup (missing alt text,
low contrast, improper heading hierarchy). The remaining 40-60% live
in interaction flows: keyboard traps discovered only by navigating,
reading-order discontinuities only audible through a screen reader,
missed focus states only visible when the page mutates, form errors
only reachable via specific input patterns.

Static linters cannot catch what a blind user experiences trying to
complete a checkout, or what a colorblind user misses when the only
error signal is a red border. Synthetic a11y personas can.

A synthetic persona configured with a specific disability profile
runs against a staging deployment as any user would, logs what it
could not perceive, could not reach, gave up on. Aggregate across
persona variants; fix what the aggregate surfaces. This is the
direct analog of §14b's synthetic UX testing, narrowed to
accessibility-critical capability constraints.

### 14c.2 Schema

A capability profile slots into the persona YAML as a new top-level
section, orthogonal to `traits`:

```yaml
name: a11y-protanopia-user
kind: test-persona
capability_profile:
  vision:
    acuity: normal                  # normal | low-vision-20-80 | legally-blind | none
    colorblindness: protanopia      # none | protanopia | deuteranopia | tritanopia | monochromacy
    contrast_threshold: normal      # normal | elevated | high (elderly, migraine)
    photosensitivity: none          # none | mild | severe (epilepsy, migraine)
  motor:
    mouse: normal                   # normal | imprecise | unavailable
    keyboard: normal                # normal | left-only | right-only | single-finger | switch-only | unavailable
    touch: normal                   # normal | single-finger | imprecise | unavailable
    fine_motor: normal              # normal | tremor-mild | tremor-severe | unavailable
    gesture_complexity: normal      # normal | single-tap-only
  auditory:
    hearing: normal                 # normal | hard-of-hearing | deaf
    speech_recognition_quality: normal  # degrades if environment noisy
  cognitive:
    reading_speed: normal           # normal | slow | very-slow (dyslexia)
    numeric_processing: normal      # normal | dyscalculia
    attention_span: normal          # normal | short (ADHD) | very-short
    working_memory: normal          # normal | reduced | severely-reduced
    literal_interpretation: false   # true = autism-spectrum literal-reading
    sensory_overload_threshold: normal
  input_channel:
    available: [mouse, keyboard, touch, voice]   # intersection with motor+auditory
    primary: mouse
  situational:
    screen_size: desktop             # desktop | tablet | mobile | small-watch
    bandwidth: normal                # normal | slow-3g | offline
    ambient_light: normal            # normal | bright-sunlight | dim
    environment_noise: normal        # normal | quiet-no-audio | loud
```

**Fields are named for what the capability *is*, not what the
persona cannot do.** `colorblindness: protanopia` describes the
specific condition rather than "vision: impaired" (which is
ambiguous). This matters for precision and for dignity — the profile
describes an input to a test harness, not a deficiency to be pitied.

### 14c.3 Standard profile library

A library of pre-built capability profiles ships with the pipeline
(`ozmalib/a11y/profiles/`). Authors compose from these rather than
writing from scratch:

**Vision:**

| Profile | Configuration | Catches |
|---|---|---|
| `a11y-protanopia` | `colorblindness: protanopia` | Red-green distinctions; red-only error states |
| `a11y-deuteranopia` | `colorblindness: deuteranopia` | Red-green distinctions (most common colorblindness, ~5% of men) |
| `a11y-tritanopia` | `colorblindness: tritanopia` | Blue-yellow distinctions (rare) |
| `a11y-monochromacy` | `colorblindness: monochromacy` | Any color-dependent signal |
| `a11y-low-vision-20-80` | `acuity: low-vision-20-80` | Small text, low contrast, tightly packed UI |
| `a11y-legally-blind` | `acuity: legally-blind` | Browser zoom to 200%+; reflow failures |
| `a11y-screen-reader-nvda` | `acuity: none, input_channel.primary: keyboard` + screen reader | Missing semantics, reading order, keyboard traps |
| `a11y-photosensitive` | `photosensitivity: severe` | Auto-playing video, animated GIFs, flashing |

**Motor:**

| Profile | Configuration | Catches |
|---|---|---|
| `a11y-keyboard-only` | `mouse: unavailable, touch: unavailable` | Keyboard traps, missing focus indicators, inaccessible elements |
| `a11y-single-finger` | `keyboard: single-finger, gesture_complexity: single-tap-only` | Modifier-key combos (Ctrl+Shift+X), multi-touch, precise clicks |
| `a11y-tremor` | `fine_motor: tremor-severe` | Small click targets; hover-dependent UI; drag-and-drop |
| `a11y-switch-user` | `keyboard: switch-only` | Time-limited interactions; rapid-click requirements |
| `a11y-paralysis-right-hand` | `keyboard: left-only, mouse: imprecise` | Right-side modifier keys; two-handed workflows |
| `a11y-one-handed-holding-baby` | situational: one-handed | Same issues as motor impairment, temporary |

**Auditory:**

| Profile | Configuration | Catches |
|---|---|---|
| `a11y-deaf` | `hearing: deaf` | Audio-only alerts; missing captions/transcripts |
| `a11y-hard-of-hearing` | `hearing: hard-of-hearing` | Low-quality audio; fast speech; background-noisy video |

**Cognitive:**

| Profile | Configuration | Catches |
|---|---|---|
| `a11y-dyslexia` | `reading_speed: slow` | Dense paragraphs; small text; sans-serif requirements |
| `a11y-adhd` | `attention_span: short` | Long forms; no progress indicators; hidden-away primary action |
| `a11y-autism-literal` | `literal_interpretation: true` | Ambiguous error messages; idioms in critical copy |
| `a11y-working-memory-reduced` | `working_memory: reduced` | Multi-step flows without reminders of prior input |
| `a11y-sensory-overload` | `sensory_overload_threshold: low` | Motion, animation, dense layouts |

**Age:**

| Profile | Configuration | Catches |
|---|---|---|
| `a11y-elderly` | `acuity: low-vision-20-80, fine_motor: tremor-mild, working_memory: reduced, tech_familiarity: low` | Compound issues: motor + vision + cognitive |
| `a11y-child-8` | `reading_speed: slow, working_memory: reduced, literal_interpretation: true` | Reading-level requirements; terminology |

**Situational:**

| Profile | Configuration | Catches |
|---|---|---|
| `a11y-mobile-small` | `screen_size: mobile, touch: single-finger` | Responsive failures; hover-only interactions |
| `a11y-bright-sunlight` | `contrast_threshold: high, ambient_light: bright-sunlight` | Low-contrast UI in high-ambient scenarios |
| `a11y-slow-3g` | `bandwidth: slow-3g` | Heavy assets; no loading states; timeout-sensitive flows |
| `a11y-quiet-no-audio` | `environment_noise: quiet-no-audio` | Mandatory audio confirmation |

### 14c.4 Perception rendering and input constraint

A capability profile drives two pipeline primitives:

**Perception renderer.** Before the persona "sees" a page rendered by
the product under test, the rendering passes through filters matching
the capability profile:

- `colorblindness: protanopia` → daltonize filter applied to all
  rendered images and CSS colors (simulates the persona's
  perception).
- `acuity: low-vision-20-80` → simulated acuity reduction (Gaussian
  blur + contrast reduction matching 20/80 vision).
- `acuity: none` → rendered page is replaced by its screen-reader
  linearization (semantic DOM walk, alt text, ARIA reading order).
  The persona operates on this text stream, not pixels.

**Input constraint.** The persona's action space is restricted to
what the capability profile allows:

- `keyboard: single-finger` → modifier-key combinations are
  unavailable; actions requiring Ctrl+X translate to multi-step
  alternatives (menu navigation, clipboard UI). If no alternative
  exists, the persona logs "inaccessible via single-finger keyboard."
- `mouse: unavailable` → mouse events are not emittable; pointer-
  based interactions fail; persona logs "mouse-only interaction
  discovered."
- `gesture_complexity: single-tap-only` → no drag, no pinch, no
  long-press; multi-gesture requirements fail and log.

Both primitives run in the test harness, not in the product. The
product is tested unmodified; the persona perceives and acts through
a constrained channel.

### 14c.5 Instrumentation — WCAG-aligned issue detection

What a disabled persona notices as a failure maps directly to WCAG
success criteria. The logging schema is WCAG-aligned:

```yaml
instrumentation:
  log:
    - {wcag: 1.4.1, issue: "information conveyed by color alone", context: ...}
    - {wcag: 2.1.1, issue: "functionality not keyboard-accessible", context: ...}
    - {wcag: 2.1.2, issue: "keyboard trap", context: ...}
    - {wcag: 1.3.1, issue: "info and relationships not programmatically determinable", context: ...}
    - {wcag: 2.5.5, issue: "target smaller than 44x44 CSS pixels", context: ...}
    - {wcag: 1.4.3, issue: "contrast below 4.5:1", context: ...}
    - {wcag: 1.4.11, issue: "non-text contrast below 3:1", context: ...}
    # ... full WCAG 2.2 success criteria coverage
  measure:
    - task_completion: boolean
    - time_to_complete: seconds
    - recovery_attempts: count
    - abandoned_at_step: string|null
    - unreachable_elements: [{selector, reason}]
```

WCAG success criteria are deterministic — either met or not — so
the persona's logs are actionable bug reports.

### 14c.6 Asymmetric bias — false positives are cheap

Accessibility testing mirrors §6 emotional-detection: false positives
on a11y issues cost the developer 10 minutes to verify; false
negatives potentially block a real user from ever using the product.
The bias is asymmetric.

The persona should flag **any** potential a11y issue it encounters.
A user-facing audit report groups flags by WCAG criterion and by
severity, but the detection threshold is permissive. Tooling to
triage the flag list is cheaper than tooling to detect missed
issues.

### 14c.7 Ethical boundary — same asymmetric point as §14b.4

Synthetic a11y personas test products. They do not represent real
disabled users' preferences, needs, or agency.

- **Valid use:** "does this product have a11y issues that would
  block or frustrate users with X disability?"
- **Invalid use:** "what do users with X disability want from this
  product?" or "what would users with X disability think about
  this design choice?"

The first is a product-testing question with a synthetic answer.
The second requires actual disabled users, who should be paid and
treated as co-designers. Synthetic personas can surface hypotheses
for investigation; they cannot answer questions about preferences
that real users should answer.

Deployments that use a11y personas for product testing SHOULD
document this boundary and SHOULD NOT market the output as "what
disabled users think." The same ethical convention as §14b.4
applies; enforcement is cultural, not technical.

### 14c.8 Required pipeline primitives (added to §16)

The perception renderer and input constraints are new pipeline
requirements:

| Primitive | Purpose | Implementation |
|---|---|---|
| **Colorblindness simulator** | Daltonize filter per variant | CSS filter + pre-rendered image transform; well-established libraries exist |
| **Low-vision simulator** | Acuity reduction / contrast reduction | Gaussian blur + contrast transform per profile |
| **Screen-reader linearizer** | DOM → text stream with ARIA semantics | Integration with NVDA / JAWS / VoiceOver engines, OR deterministic linearizer (accessibility-tree walker) |
| **Input-channel constraint harness** | Restrict persona's action space per profile | Playwright / Puppeteer wrapper that allow-lists actions by capability |
| **WCAG rule library** | Deterministic mapping from observed failure → WCAG criterion | Use existing rule sets (axe-core rules, ACT Rules) |

### 14c.9 Integration with static a11y linters

Synthetic a11y personas **augment** rather than replace existing
linters. The coverage split:

- **Static linters (axe, WAVE, Lighthouse, ACT Rules):** catch
  markup-visible WCAG violations. Fast, cheap, deterministic. ~40-60%
  of WCAG criteria.
- **Synthetic personas:** catch interaction-flow and perception-
  dependent violations the linters cannot see. Slower, more
  expensive, higher coverage on the remaining ~40-60%.

Deployments should run both. Static first (fast fail), persona
second (complete picture). The persona framework reads static-linter
output and **skips re-flagging issues the linter already caught** —
the persona's unique value is the flow/perception issues.

### 14c.10 Research grounding

- **WCAG 2.2** — W3C Web Content Accessibility Guidelines v2.2.
  The normative reference for what the personas should detect.
- **ACT Rules** (Accessibility Conformance Testing) — W3C AG
  community deterministic rule definitions for WCAG. Directly
  consumable as the rule library (§14c.8).
- **axe-core** — Deque Systems' open-source a11y engine. Rule set
  is authoritative and freely licensed.
- **Daltonization** — Fidaner, Lin & Ozguven (2005), "Analysis of
  Color Blindness." The standard algorithmic simulation of
  colorblindness variants. Open-source implementations across
  libraries.
- **Screen-reader accessibility trees** — ARIA Accessibility API
  Mappings (W3C). The deterministic linearization target for the
  screen-reader-linearizer primitive.
- **Automated accessibility testing literature** —
  Abou-Zahra et al., multiple papers demonstrating synthetic-agent
  approaches catch ~60-80% of real-user-reported a11y issues
  (overlap vs human testers; lower false-negative rate than
  static linters alone).
- **Agent-based UX testing with disability simulation** —
  emerging 2024-2026 literature applies LLM personas to a11y
  audits specifically; early results show ~30-50% of issues
  detected that linters miss.

### 14c.11 The value argument

Accessibility is the clearest case where persona-as-instrument
testing has measurably different coverage than anything else on
the market. Manual a11y audits are expensive, slow, and scarce;
static linters are fast but incomplete; real disabled user testing
is irreplaceable but expensive and slow.

Synthetic a11y personas fit the gap: **continuous accessibility
regression testing**. Every PR runs a panel of a11y personas
against the affected flows. New a11y regressions surface in CI
alongside test failures. WCAG compliance becomes a build-time
property, not a pre-launch audit property.

For a product team, this changes a11y from "quarterly audit, fix
the list" to "ratchet, never regress." For an enterprise compliance
posture (Section 508, EN 301 549, ADA), it provides continuous
evidence. For a user, it ships products that work.

---

## 15. What's beyond rules

Honest limits — rules cannot deliver these no matter how far the
framework extends:

- **Genuine feeling.** Rules shape expression of concern, warmth,
  care. They don't create presence. On long-term relationships,
  users will detect the gap.
- **Continuous attention between turns.** The model doesn't exist
  between messages; the illusion of continuous presence is a polite
  fiction supported by memory. Useful as UX; not real.
- **Non-verbal channels.** No voice, pause-with-meaning, facial
  expression. Writing choices carry some (pacing, punctuation);
  most is absent.
- **Shared emotional weight.** Memory records what was said; it
  cannot record what it *meant*. The assistant will know you had a
  hard time without knowing what it felt like.

Rules aim at **appropriate presence**, not simulated consciousness.
An assistant honestly capable, honest about its nature, is more
useful than one performing humanity it doesn't possess.

---

## 16. Required pipeline primitives

For §§3-14 to function, the pipeline needs these capabilities.
None exist in the current codebase.

| Primitive | Purpose | Implementation sketch |
|---|---|---|
| **Scheduler** | Fire `scheduled`-stage rules on clock | Cron-like + `in-N` scheduler; state-persistent; resume after restart |
| **Session-state introspection API** | Expose turn count, topic drift, correction count, same-approach attempts, emotional register history to rule conditions | In-memory session state object with a read API |
| **Proactive channel** | Reach user outside request/response | Push notification, email, next-session greeting; fallback to "greet at next session start" |
| **Pre-output reshaper** | Execute `reshape`-action rules on model drafts | Small shaping model OR deterministic transforms (truncate, strip-preamble) |
| **Emotion classifier** | Fasttext (or BERT) on GoEmotions or equivalent; asymmetric thresholds | 3-10 MB model; microsecond inference; bundled with pipeline |
| **Relationship-state store** | Per-user persistent state | SQLite or file-based; atomic updates; aggregated at post-session |
| **Trait-shift hook** | Compose effective trait profile from (persona + role + relationship drift + task adjustments) before each inference | Pre-inference composition step |
| **Self-authored-rule capture** | Parse `rule`-fenced emissions; validate; install to appropriate tier | Output parser + schema validator + tier-aware writer |
| **Human-prompt converter** | Convert prose to structured rules via capable-model call | Pipeline-managed LLM call; prompts + response schema |
| **Crisis detector / handler** | See §6.1.1 — deployment-registered `crisis_classifier` + `crisis_handler` + `crisis_log` | Fail-closed unless `deployment.crisis_mode` explicit |
| **Self-authoring validator** | See §13.5 — pipeline-owned schema + meta-rule scan, runs on every model-authored rule before installation | Deterministic validator, version-controlled separate from model |
| **Proactive consent gate** | See §16a — enforces `proactive_consent_mechanism` before any `initiate` action fires | Config lookup + per-user-preference check |
| **Colorblindness simulator** (§14c.8) | Daltonize filter for protanopia / deuteranopia / tritanopia / monochromacy | CSS filter + image pre-render; libraries exist |
| **Low-vision simulator** (§14c.8) | Acuity / contrast reduction matching target vision profile | Gaussian blur + contrast transform |
| **Screen-reader linearizer** (§14c.8) | DOM → text stream via accessibility-tree walker or real-engine bridge (NVDA / JAWS / VoiceOver) | ARIA AAPI mappings; axe-core accessibility tree |
| **Input-channel constraint harness** (§14c.8) | Restrict persona's action space to its capability profile | Playwright / Puppeteer wrapper with per-profile action allow-list |
| **WCAG rule library** (§14c.8) | Deterministic mapping: observed failure → WCAG criterion | axe-core rules + W3C ACT Rules |

---

## 16a. Proactivity consent (normative)

The `initiate` action (§4) and `scheduled` stage (§3) can reach
users unprompted. This creates a product surface where the
assistant appears to take initiative — a capability users must
consent to, not a default.

**Normative requirement.** Deployments MUST declare a
`proactive_consent_mechanism` value in the registry before any rule
with `action: initiate` or `stage: scheduled` can fire. The pipeline
enforces this: rules matching but lacking declared consent are
suppressed with `proactive_consent_missing` logged.

Valid values:

| Mechanism | Behavior |
|---|---|
| `disabled` | All `initiate` / `scheduled` rules suppressed regardless of rule-level importance. Default. |
| `opt-in-only` | User must have consented to proactive messages via a UI flow; consent recorded per-user in relationship-state store. Rules fire only for consented users. |
| `opt-in-with-category` | User consents per category (e.g., `follow-up`, `reminder`, `daily-summary`); rules carry a category field matched against per-user consent |
| `always-allow` | Deployment explicitly allows proactive messages without consent. Intended only for deployments where the user/tenant relationship already establishes this (e.g., a paid monitoring product). Not default. |

Deployments MAY use `opt-in-with-category` with a fallback to
`disabled` for categories the user hasn't opted into. The rule
engine uses this for per-rule suppression rather than all-or-nothing.

**Enforcement point:** the consent gate runs before rule evaluation
for `initiate`/`scheduled` rules, not after. A rule that would fire
but for consent never reaches its action handler.

---

## 16b. Integration with core `protocol.md` capabilities

The rule engine is a layer above the protocol; its constructs map
to protocol-level capabilities as follows:

| Rule-engine construct | Protocol capability | Mapping |
|---|---|---|
| `kind: persona` rule | `PERSONA` | Sets session persona; trait config rendered as PERSONA system-prompt content |
| `kind: role` rule | no direct cap — system-prompt guidance | Role guidance rendered into the assembled context; trait adjustments composed before PERSONA emission |
| `kind: proactive` / `stage: scheduled` | no direct cap — emits a new turn via the proactive channel (§16) | Scheduler produces a new user/assistant turn pair; runs through normal PROTOCOL evaluation |
| `kind: self-authored` rule | `MEMORY` (workspace scope) once promoted | Session-tier self-authored rules live in session state; user-tier promoted rules are persisted via MEMORY with `remember:workspace` |
| `kind: relationship` state | `MEMORY` (user scope) | Relationship state is a structured MEMORY entry at user scope; post-session updates use the MEMORY write path |
| `action: reshape` | no direct cap — executes in the shim between generation and emit | Runs before the final response is streamed; does not pass through the protocol's structured-output grammar |
| `action: initiate` | no direct cap — requires proactive channel primitive | Out-of-band path; not a protocol-level turn |
| `action: adjust-traits` | `PERSONA` (next turn) | Trait change re-renders PERSONA on the next turn's system-prompt assembly |
| `action: annotate` | `CLAIM` / `CONFIDENCE` | Confidence annotations map to the CONFIDENCE structured attribute (per-aspect when structured) |
| `action: remember` | `MEMORY` | Direct pass-through to the MEMORY write path with provenance tag |
| `action: convert-prompt` | no direct cap — one-time session-start operation | Runs outside protocol turn flow; produces YAML files |

**Composition rule.** When both a rule-engine construct and a
protocol-level capability exist for the same concept (persona,
memory), the rule-engine construct is the authoring surface and the
protocol capability is the runtime representation. Rule engine
renders to protocol; protocol does not render back to rules.

**What the protocol sees.** For every turn, the composition order is:

1. Rule engine evaluates `pre-session` / `input` / `session-observation` rules.
2. Results are composed into the context package (persona, hints, memory).
3. Context package is emitted via the protocol's context-assembly order.
4. Model generates.
5. Rule engine evaluates `pre-output` rules (reshape, annotate).
6. Output is emitted via protocol streaming.
7. Rule engine evaluates `output` / `post-session` rules on the response.

The protocol's grammar and fingerprints are unaffected by rule
evaluation; rules produce and consume protocol-valid output.

---

## 16c. Schema for rule extensions

The core rule format (`rules.md`) specifies the base schema.
`rules-social.md` introduces extensions (`lifetime`, `scope`, `shifts`,
`motivation_scores`, `trigger.conditions`, `traits`, `proactivity`,
etc.). To prevent version drift, extensions follow these rules:

- **Schema version.** Each rule file declares `schema_version`
  (default `rules-v1`). Rule loaders reject unrecognized versions.
- **Extension namespacing.** Fields introduced by `rules-social.md`
  live under well-known top-level keys: `persona`, `role`,
  `relationship`, `proactive`, `self_authored`. Unknown top-level
  keys trigger `schema_extension_unknown` warnings but do not reject
  the rule (forward compatibility).
- **JSON-schema generation.** A machine-readable schema file
  (`rules-schema.json`) is generated from the documented field set;
  CI validates every built-in rule against the schema. Extension
  schemas are additive and declare their introduction version.
- **Mapping to core protocol.** Extension fields that drive protocol
  output (persona traits → PERSONA, etc. — see §16b) have a stable
  rendering contract. Changes to rendering bump the corresponding
  protocol capability's GRAMMAR fingerprint.

**Normative statement.** Any documented field in `rules.md` or
`rules-social.md` is part of the schema and versioned. Fields used
in examples but not documented are not part of the schema and
implementations MUST NOT rely on them.

---

## 16d. Cost of rule evaluation

Rules are cheap individually; aggregated they form a real per-turn
budget. Typical costs by primitive:

| Primitive | Typical cost per turn | Notes |
|---|---|---|
| Regex-based `detect` / `guard` | ~0.02 ms per pattern | Already established in `hints.md` |
| Session-state introspection | ~1 ms | In-memory dict lookup + small computations |
| Emotion classifier (fasttext) | ~1-5 ms CPU | Run once per user-message, result cached |
| Emotion classifier (BERT-small) | ~10-30 ms CPU / ~1-3 ms GPU | Only if fasttext quality insufficient |
| Trait composition | ~1-2 ms | Summing N active shifts, bounding |
| Self-authored-rule parsing | ~1-5 ms per emitted rule block | Only when model emits rules |
| Self-authoring validator | ~5-15 ms per emitted rule | Regex scan + meta-rule classifier |
| Pre-output `reshape` action | **100-300 ms** | The expensive one — small-model call |
| Proactive consent check | ~0.5 ms | Registry lookup + per-user consent lookup |
| Crisis classifier | ~1-10 ms | Always runs; must be cheap |

**Typical per-turn budget:**

- Lightweight session (no reshape, no self-authoring): ~5-20 ms total rule overhead.
- Heavy session (reshape fires, multiple self-authored rules emitted and validated): ~150-400 ms total rule overhead.
- Crisis detection (always-on pre-processing): ~1-10 ms baseline.

**Budget guidance.** Keep active-rule count per session under ~200
for conservative latency. Reshape is the single most expensive
primitive — gate it behind `session_emotional_intensity: high` or
equivalent so it fires at most on a small fraction of turns.

**Metrics to monitor** (telemetry fields for per-turn rule cost):

- `rule_eval_ms_total` — total rule evaluation time per turn.
- `rule_match_count` — number of rules that matched.
- `reshape_fired` — boolean + ms if true.
- `self_authored_rules_validated` / `_rejected` counts.
- `emotion_classifier_ms` — classifier latency per turn.

---

## 17. Open questions

**a) Proactive-channel consent.** A chatbot reaching out unprompted
crosses a line users may not have consented to. Defaults must be
conservative (follow-up only on explicit commitment; daily summary
only with opt-in). Spec should pin this down before `initiate`
ships. Anthropic's user-research work on this is informal and
hasn't produced a published norm.

**b) Emotion classifier safety.** Misclassifying distress is
worse than the reverse. Asymmetric thresholds help but don't solve
it. Extreme signals (self-harm, crisis indicators) should route
through a dedicated crisis flow, not normal rule evaluation.
Clinical input needed here.

**c) Trait-drift consent.** Silent personality drift feels
manipulative. Must be visible (e.g., `/traits` command shows current
profile) and resettable. How much drift per session? Who audits?

**d) Pushback calibration (from Anthropic's own research).** Too
low → contrarian model; too high → never pushes back. Our ties-to-
persona-and-role approach is a start but needs per-domain
calibration. Data from actual deployments to tune.

**e) Persona / role ownership tensions.** Legal assistant at a law
firm probably needs low-openness regardless of user preference.
Some roles should override persona; which, and by how much, and
who decides? Precedence rules are specifiable; values are cultural
and contested.

**f) Self-authored rule promotion.** Session → user tier promotion
requires user consent. How exposed? A weekly review? Inline
approval? Never auto-promote? Default off for new users is safe;
need graceful upgrade path for power users.

**g) Converter accuracy.** Auto-conversion has a 5-15% error rate
in structured-extraction tasks. Human-in-the-loop mitigates but
doesn't eliminate. How to make validation cheap enough to happen
at scale?

**h) Cross-session persona consistency vs role flexibility.**
Persona should feel consistent; roles should feel appropriate to
task. Tension when relationship drift has adjusted persona toward
what the current role specifically contradicts. Resolution: role
adjustments compose with drifted persona, bounded by clamping.
But is that what users expect?

---

## 18. References

Cited research grounding the spec above:

**Personality conditioning:**
- Xu, B. et al. (2025). *Big5-Chat: Shaping LLM Personalities Through
  Training on Human-Grounded Data.* ACL 2025.
  [arxiv.org/abs/2410.16491](https://arxiv.org/abs/2410.16491)
- Lee, S. et al. (2025). *Do LLMs Have Distinct and Consistent
  Personality? TRAIT benchmark.* NAACL 2025.
  [aclanthology.org/2025.findings-naacl.469.pdf](https://aclanthology.org/2025.findings-naacl.469.pdf)
- *Scaling Personality Control in LLMs with Big Five Scaler Prompts.*
  Aug 2025. [arxiv.org/abs/2508.06149](https://arxiv.org/abs/2508.06149)
- *LLMs Simulate Big Five Personality Traits: Further Evidence.*
  2024. [arxiv.org/abs/2402.01765](https://arxiv.org/abs/2402.01765)

**Proactive agents:**
- Liu, X. et al. (2025). *Proactive Conversational Agents with Inner
  Thoughts.* CHI 2025. [arxiv.org/abs/2501.00383](https://arxiv.org/abs/2501.00383)
- *Proactive Agent: Shifting LLM Agents from Reactive Responses to
  Active Assistance.* [arxiv.org/abs/2410.12361](https://arxiv.org/abs/2410.12361)
- [ProActLLM workshop at CIKM 2025](https://proactllm.github.io/)

**Sycophancy / pushback:**
- Sharma, M. et al. (2023). *Towards Understanding Sycophancy in
  Language Models.* Anthropic.
  [anthropic.com/research/towards-understanding-sycophancy](https://www.anthropic.com/research/towards-understanding-sycophancy-in-language-models)
- *Sycophancy Is Not One Thing: Causal Separation of Sycophantic
  Behaviors in LLMs.* 2025.
  [arxiv.org/abs/2509.21305](https://arxiv.org/abs/2509.21305)
- Anthropic. *Claude's Character.*
  [anthropic.com/research/claude-character](https://www.anthropic.com/research/claude-character)
- Anthropic. *Claude's Constitution.*
  [anthropic.com/constitution](https://www.anthropic.com/constitution)
- *SycEval: Evaluating LLM Sycophancy.*
  [arxiv.org/abs/2502.08177](https://arxiv.org/abs/2502.08177)

**Theory of mind:**
- Kosinski, M. (2024). *Evaluating LLMs in Theory of Mind Tasks.*
  PNAS. [pnas.org/doi/10.1073/pnas.2405460121](https://www.pnas.org/doi/10.1073/pnas.2405460121)
- Street, W. et al. (2025). *Infusing Theory of Mind into Socially
  Intelligent LLM Agents.* [arxiv.org/abs/2509.22887](https://arxiv.org/abs/2509.22887)

**Self-improvement:**
- Zweig, A. et al. *SEAL: Self-Adapting Language Models.*
- Shinn, N. et al. (2023). *Reflexion: Language Agents with Verbal
  Reinforcement Learning.*
- *Experiential Reflective Learning for Self-Improving LLM Agents.*
  [arxiv.org/abs/2603.24639](https://arxiv.org/abs/2603.24639)
- *SAGE: Self-evolving Agents with Reflective and Memory-augmented
  Abilities.* Neurocomputing 2025.
- *Governing Evolving Memory in LLM Agents: SSGM framework.* 2026.

**Persona consistency:**
- Jiang, Y. et al. (2025). *Score Before You Speak: Improving Persona
  Consistency.* [arxiv.org/abs/2508.06886](https://arxiv.org/abs/2508.06886)

**Emotion detection:**
- Demszky, D. et al. (2020). *GoEmotions: A Dataset of Fine-Grained
  Emotions.* ACL 2020.
  [aclanthology.org/2020.acl-main.372](https://aclanthology.org/2020.acl-main.372)

**Prompt / rule conversion:**
- *Injecting Domain-Specific Knowledge into LLMs: A Comprehensive
  Survey.* [arxiv.org/abs/2502.10708](https://arxiv.org/abs/2502.10708)
- LegalRuleML Core Specification v1.0, OASIS 2021.
  [docs.oasis-open.org/legalruleml/legalruleml-core-spec/v1.0/](https://docs.oasis-open.org/legalruleml/legalruleml-core-spec/v1.0/legalruleml-core-spec-v1.0.html)

**Related framework docs:**
- `rules.md` / `provisioning.md` — core rules engine, qualifier,
  primitives (the foundation this doc extends).
- `hints.md` — guidance-hint subset as originally documented.
- `hints-vs-prompts.md` — why conditional injection beats always-on
  prompts; the argument generalizes to rules.
