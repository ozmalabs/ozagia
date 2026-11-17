# Foundation

**Version:** 0.2
**Status:** Axiomatic — foundational to all Ozma protocol documents.
**Date:** 2026-04-23
**Companion specs:** [`protocol.md`](./protocol.md) — Model Language; [`shim.md`](./shim.md) — Shim Requirements; [`maps.md`](./maps.md) — Map Format

---

## The axiom

**You are not talking to something. You are instructing a machine.**

Every LLM interaction is a function call, not a conversation. The
system accepts context as input, computes a conditional distribution
over next tokens, and samples from it. That is the entire operation.

Everything else — "the model said," "it thinks," "it remembers,"
"it's feeling" — is a projection of human conversational patterns
onto a computing surface. The projection is useful as a UX for
untrained users, but it is false as a description of the system.

This document, and every other Ozma protocol document, is written
from the programming-model stance. The conversational illusion is
a presentation layer built on top of the protocol; it is not the
protocol.

**Large Semantic Models (LSMs).** The Ozma protocol targets a specific
architectural class: models that read, write, and compose semantic
maps as their native data structure. A map-literate model does not
need per-format training; it needs the schema. The protocol's entire
job is to generate, transmit, cache, and apply maps. See
[`maps.md`](./maps.md) for the map format specification and
[`protocol.md`](./protocol.md) for the model language built on it.

---

## Why it matters

Most confusion about LLM behavior resolves once the axiom is
accepted:

| User-facing confusion | Under the axiom |
|---|---|
| "It forgot what we talked about yesterday" | Yesterday isn't in its context. Provide a context that includes it (memory). |
| "It contradicted itself in one reply" | Each token is sampled from a distribution, not drawn from a consistent internal belief. |
| "It lied to me" | Token probabilities don't have a truth predicate. Provide context that grounds truth (reference lookups). |
| "It's being sycophantic" | Training rewarded agreement; this is the pattern in the weights. Provide context that rewards pushback (anti-sycophancy rules). |
| "It doesn't understand what I want" | Its context is incomplete. Provide context that specifies intent (hints, current-data, persona config). |
| "It's inconsistent across sessions" | Sessions share no state by default. Provide shared state (relationship memory). |

Each failure mode is a missing-context problem. The protocol's job
is to supply the right context. The assistant's failures are almost
never "the model's fault"; they're context-engineering failures at
the protocol layer.

---

## What this implies about the design

Once the axiom is accepted, the architectural choices throughout the
Ozma spec are not inventions — they are the obvious consequences:

- **Context is the program.** If output depends on input, input
  quality determines output quality. "Prompt engineering" is just
  "programming for a machine whose instruction set is natural
  language patterns."
- **Rules have qualifiers the model reads.** You can specify behavior
  precisely because the machine follows its input. Graded
  importance is honored if you tell the machine the grades.
- **Persona is configuration, not identity.** There is nothing to
  have identity. There is a parameter set that shapes output
  distribution. Configurable, auditable, versionable, swappable.
- **Memory is external state.** The machine has no persistence
  mechanism. The pipeline provides one.
- **Current data is a hint kind.** The machine has no clock, no
  filesystem, no network — it has input tokens. Information about
  the current world must arrive as input.
- **Rules can be mined / authored / model-emitted.** The machine
  follows structured instruction, regardless of who authored the
  structure. Humans, extraction pipelines, and the model itself
  can all produce rules. All three are valid sources; provenance
  determines trust.
- **Measurement is possible.** Deterministic input should produce
  bounded-variance output. Output quality with and without a given
  rule is comparable. The harness work (HumanEval, corpus A/B, the
  matrix experiment) is not novel — it's basic software engineering
  applied to the right conceptual object.
- **Personas are instruments.** A configurable parameter set can be
  instantiated many times. If the user-facing product is a chatbot,
  fine. If the product is a UX testing tool, also fine. Same
  machine; different configurations.
- **The protocol is mechanistic; the UX is conversational.** Two
  layers, two vocabularies. The middle layer (YAML, rules, fenced
  hint blocks) is mechanical; the author-facing tools and
  user-facing surfaces can translate to human metaphors. The
  translation is well-defined and one-directional — metaphor
  never leaks back into the protocol.

---

## What this implies about product

Two audiences, two framings, one protocol:

- **Technical users** (developers, engineers, ML practitioners)
  interact with the protocol directly. They understand rules as
  conditional instructions. They write YAML, measure lift, version
  libraries. For them, Ozma's architectural honesty is a feature:
  the system behaves the way they expect a programmable tool to
  behave.

- **End users** interact with a conversational surface built on top
  of the protocol. Their assistant has a name, expresses
  preferences, remembers commitments, follows up. They never see
  the YAML. The translation from protocol to conversational UX is
  clean because the protocol is mechanical: "the assistant remembers
  X" maps to "the memory rule for X fired and was injected into
  context." No pretense that the machine is more than it is.

Products that optimize for one framing and sneak the other in —
marketing an entity while engineering a function approximator, or
marketing a function approximator while trying to make it behave
like an entity — create technical debt on both sides. Honest layering
resolves this.

---

## What this implies about failure modes

When the protocol produces bad output, the question is always:
**what was missing from the context?** Not "what did the model do
wrong?" Not "why is it being difficult?" Not "how do we fix the
model's attitude?"

Debugging proceeds mechanically: trace which rules fired, which
didn't, what got injected, what the provenance and importance
looked like. If a security rule failed to fire because its detect
pattern had a typo, that's a rule bug. If a persona shifted
incorrectly because the wrong role was active, that's a composition
bug. If the model contradicted a known fact, that's a reference hint
that should have fired and didn't.

None of these are "the model's fault." All of them are problems in
the context the pipeline assembled. The pipeline is the program;
the model is the interpreter. Interpreter bugs exist (tokenization,
attention pathologies, training data artifacts), but the overwhelming
majority of observed bad output is program bugs — things the pipeline
failed to put in context, or put in context wrongly.

---

## Consequences for documentation style

Every document in the Ozma protocol tree adheres to these conventions:

1. **No anthropomorphization in normative text.** Say "the rule fires
   when condition X matches and its action adjusts parameter Y,"
   not "the model notices X and responds with Y."
2. **Human metaphors are labeled as translation layer.** When human
   vocabulary appears in the spec (inevitable for discussing
   persona, register, relationship), it's marked as UX-surface
   terminology, not protocol semantics.
3. **"The model" is singular, mechanistic, replaceable.** Not an
   agent with preferences. If different models behave differently
   under the same protocol, that's a capability ceiling observation,
   not a personality difference.
4. **Failure modes are context bugs first.** If the system misbehaves,
   check the context assembly before concluding the model is
   inadequate.
5. **User-facing and protocol-facing documentation are explicitly
   separated.** This document is protocol-facing. A user-facing
   document about the same feature can use conversational framing
   freely, as long as it's clear which layer it's describing.

### Reconciling with `rules-social.md`

`rules-social.md` uses trait vocabulary (OCEAN: Openness,
Conscientiousness, Extraversion, Agreeableness, Neuroticism),
"emotional register," "relationship state," and related
human-interpretable terms. This is not a contradiction of the
axiom; it is the translation layer working as designed.

- **At the authoring surface** (YAML file contents, UI sliders,
  author documentation): trait vocabulary is used because authors
  reason in those terms. Lowers the authoring barrier.
- **At the protocol layer** (rule evaluation, parameter composition,
  grammar output): everything reduces to rule-gated parameter
  adjustments and declarative conditionals. No interior states.
- **At the user-facing surface** (product UI, conversational output):
  trait vocabulary may appear freely because that's the user's
  mental model.

The contract is one-directional: metaphor leaks *from* authoring and
user surfaces *into* generated guidance text, but never *back into*
the protocol semantics. `rules-social.md` §10.4–10.6 make this
distinction explicit within that document; this paragraph declares
it globally.

---

## The aphorism

If the axiom compresses further:

> **The LLM is a program whose instructions are natural language
> patterns assembled by the pipeline.**

The pipeline is the programmer. The protocol specifies the
instruction format. The model is the runtime. Everything else —
personas, memories, conversations, agents — is nouns we assign to
configurations for human comprehension. Useful nouns, but nouns,
not ontology.
