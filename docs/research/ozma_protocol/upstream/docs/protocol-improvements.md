# Ozma Protocol — Proposed Improvements

**Author:** Claude (Opus 4.7)
**Date:** 2026-04-18
**Companion to:** `protocol.md`, `protocol-review.md`

The review document covers correctness and robustness fixes to the existing v1 draft. This document is forward-looking: what's missing, what compression methods could be layered in, and where the step-changes live.

---

## The biggest structural gap: it's a per-turn protocol, not a session protocol

Everything compresses *within a request*. Across a 50-turn agent session the outline gets re-sent, the call graph re-rendered, logs re-compressed. The dominant cost in long agent loops isn't any single turn — it's repetition.

### Content-addressable chunks

The shim maintains a per-session cache keyed by SHA of each compressed chunk. First time the model sees `config.py`'s outline, it's rendered in full with a fingerprint:

```
{@a4f9c1 = outline of config.py}
[1] module config
[2]   class Config
...
```

Next turn, the context just says `{@a4f9c1}`. The LoRA is trained to accept these tokens as expanded context — the shim fills them in on the way in and strips them on the way out. Think of it as model-side prompt caching but explicit and content-addressed instead of prefix-matched.

This enables three things that aren't possible today:

- **Anaphoric references:** `@prev`, `@target`, `@last_edit` — the model says "same as before" in two tokens.
- **Cross-turn deltas:** "outline of config.py: same as `@a4f9c1` except [6] is now modified" instead of re-sending the whole thing.
- **True sparse context:** at scale you keep hundreds of addressable chunks live across a session at near-zero per-turn cost.

On a 50-turn session, expect **2-4x cost reduction** on top of what the current protocol already delivers. This one change matters more than any other single addition.

---

## Missing data types

### CLAIM / INVARIANT

The protocol says *what to change*. It doesn't say *what must be true*. A model emitting:

```
claim [6]: returns {} when file is empty
claim [6]: never raises on malformed JSON
```

is machine-verifiable against tests, property checks, or type-level assertions. This shifts the protocol from imperative ("apply this edit") toward contractual ("I guarantee this invariant now holds"). Enables much higher autonomy — the outer loop can trust claims that were verified vs. claims that weren't, and prioritize correction on unverified ones.

### TYPE

Dedicated compressed type notation. `Dict[str, List[Tuple[int, Callable[[X], Y]]]]` is brutal. A type IR with de Bruijn indices, named type aliases, and back-references could cut type-heavy code (TypeScript, Rust, Haskell, heavily-annotated Python) by another 30-50% on top of IR.

Also enables TYPE-level edits: "change the return type of [6] from A to B" without touching the body.

### CONFIDENCE

First-class on every output, not just HINT:

```
edit [6] confidence=0.9
  ...
edit [8] confidence=0.4
  ...
```

Lets the orchestrator decide which edits to apply immediately vs queue for review. The retry loop becomes "retry only low-confidence edits" instead of "retry everything."

### REASONING (distinct from WIRE)

Modern models emit long thinking blocks. WIRE targets output prose. Reasoning is different — it's conditional, branchy, and a lot of it is dead weight ("let me think... actually no..."). A REASONING format: keep conclusions and pivot points, drop dead branches.

Structured reasoning format:

```
reasoning:
  h1: empty file → json.load raises → wrap in try? → reject ∵ silent failure
  h2: check file size first → accept ∴ edit [6] with size guard
```

The LoRA is trained to emit structured reasoning rather than free-form thinking. Cuts reasoning tokens maybe **3-5x**.

### PLAN

Multi-step plans as a structured type rather than prose:

```
plan:
  [1] expand [6]
  [2] verify callers
  [3] edit [6] with guard
  [4] run tests [6]
```

Each step is executable by the orchestrator, checkable for progress, and referenceable in later turns.

### TEST-SPEC

Test intent as a compressed form, not just test bodies:

```
test-spec [6]:
  empty file → {}
  malformed JSON → raises
  5MB file → succeeds
```

The LoRA can emit these as a byproduct of edits; the orchestrator can generate actual test code from them via a cheaper model.

---

## Compression methods worth adding

### Repetition / pattern extraction

20 similar handlers compress to:

```
pattern P1: ƒ handle_{name}({args}) → Response: ...
  handle_user(user: User)
  handle_post(post: Post)
  handle_comment(comment: Comment)
  ... 17 more
```

For route-heavy web code, test suites, and CRUD modules this is a **5-20x win**. The Tree-sitter stubber already sees structure; the next step is cross-function structural dedup.

### Cross-file structural sharing

If `config.py` imports are already shown, `main.py`'s imports from config reference the existing chunk instead of re-listing. DAG-level compression on top of tree-level compression.

### Attention-aware compression driven by telemetry

Positions the model consistently ignores get compressed harder next time. Positions it expands often get kept at full fidelity. Needs the telemetry schema from the review document — but once you have it, this is a continuous improvement lever with no model retraining.

### Docstring / comment compression

Docstrings often 2-5x longer than the code. Keep signature, type info, and example *shapes*. Drop motivation prose. Sometimes partially handled by WIRE today; a docstring-specific format would do much better.

### Learned BPE extension

Train a tokenizer extension on the target repo's code. `cursor.execute` becomes one token. `async def __init__` becomes one token. Requires tokenizer surgery but is compatible with the LoRA if done pre-training. Expect **15-25%** on top of everything else.

### Embedding-based retrieval as a protocol primitive

Current `search "pattern"` is lexical. Add:

```
search semantic: "empty file handling"
```

that returns positions by embedding similarity. Lets the model ask for semantically related code it doesn't know exists. Reduces hallucination meaningfully on cross-module reasoning.

---

## Flexibility gaps

### Streaming / progressive edits

Current EDIT is block-based — model holds the whole patch in its context before emitting. For large refactors, streaming edits (commit as they arrive, validate incrementally, rollback on failure) gives the model more slack to plan and the system earlier feedback.

### Transactional edit groups

```
transaction T1 {
  edit [6]
  edit [9]
  edit [12]
}
```

All-or-nothing. Right now edits are independent; partial success leaves the tree in weird states that the correction loop then has to figure out.

### Multi-model handoff

If the pipeline routes phases to different models, the protocol needs explicit handoff:

```
handoff from=model-a to=model-b:
  concluded: X confidence=0.8
  open questions: [1] how to handle Y, [2] signature of Z
```

Currently the pipeline does this out-of-band; making it in-band lets the model reason about its predecessor's work.

### Dry-run / preview

```
preview edit [6]
```

Model emits the edit, shim computes the resulting diff, returns it, but doesn't apply. Lets the model self-check without mutating. Especially useful in long agent loops to avoid accumulating mistakes.

### Constrained decoding

Not a protocol change, a decoding-side change: the LoRA emits only tokens that are valid at the current grammar state. Parse failures go to zero. Format-validity goes to 100%. This is the single biggest quality lift available from tooling (Outlines / llguidance / Guidance / xgrammar) and the protocol should assume it's in use.

---

## Integration with what's already built

- **Rules engine as a HINT producer.** Rules already emit scored matches. Route them into the HINT data type directly — one less translation layer, and the LoRA gets trained on rule-produced hints as legitimate targets.
- **Checks as structured ERROR output.** The Scope Check feedback is prose-like today. A formal ERROR type with `{kind, position, severity, fixup_hint}` enables the LoRA to pattern-match check feedback robustly.
- **Training-data capture as a protocol primitive.** `X-Ozma-Training: opt-in` header — sessions with this header become training data (with claim/edit/verification outcomes as labels). Closes the loop between production and the next LoRA version.

---

## Where the step-changes are

If the goal is "another order of magnitude" rather than "another 20%", the two highest-leverage additions are:

1. **Session-level content-addressable caching.** Makes the protocol stateful and sparse. Biggest practical cost win on real workloads.
2. **CLAIM + CONFIDENCE as first-class types.** Moves the protocol from "compressed imperative edits" to "verifiable contracts with uncertainty." This is what lets the LoRA drive *trustworthy* automation, not just *cheap* automation — and it's probably what separates Ozma from "better tokenizer for code."

Everything else is worth doing but stacks additively. Those two change what the system is, not just how well it does the same thing.

---

## Rough priority ordering

| Priority | Addition | Type of win |
|----------|----------|-------------|
| P0 | Session-level content-addressable caching | Cost (step-change on long sessions) |
| P0 | CLAIM / INVARIANT type | Trust / autonomy (step-change) |
| P0 | CONFIDENCE on all output types | Quality, retry efficiency |
| P1 | Constrained decoding (decode-time) | Quality (format validity → 100%) |
| P1 | REASONING data type | Cost on thinking-heavy models |
| P1 | Pattern extraction compression | Cost on CRUD-heavy code |
| P1 | TYPE data type | Cost on type-heavy languages |
| P2 | PLAN data type | Orchestration quality |
| P2 | TEST-SPEC data type | Coverage automation |
| P2 | Transactional edit groups | Correctness on multi-edit changes |
| P2 | Streaming / preview edits | Latency + safety |
| P2 | Multi-model handoff primitives | Pipeline efficiency |
| P3 | Attention-aware compression | Continuous improvement lever |
| P3 | Cross-file structural sharing | Cost on large repos |
| P3 | Docstring-specific compression | Cost on heavily-documented code |
| P3 | Learned BPE extension | Cost (requires tokenizer surgery) |
| P3 | Semantic search as protocol primitive | Hallucination reduction |
| P3 | Training-data capture primitive | Feedback loop |
