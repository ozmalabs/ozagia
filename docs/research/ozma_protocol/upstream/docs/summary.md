# Ozma

**Ozma places contracts on LLM coding.**

Every LLM coding assistant today operates the same way: write a prompt, hope the output is correct, eyeball the diff. There are no contracts, no typed I/O, no verification. The model either does what you meant or it doesn't, and finding out which requires reading the code.

The interface to LLMs has barely changed since 2023. The Chat Completions API launched in March 2023 with a format — role/content message arrays, freeform system prompts, JSON Schema tool definitions — that every major provider then copied. Anthropic, Google, Mistral all converged on the same abstraction independently. A developer from that launch would recognise every API call being made today.

The models underneath have not stood still. HumanEval pass rates went from 46% to 97%. SWE-bench — real software engineering tasks on real codebases — went from 20% to over 70% in under a year. Mathematical reasoning went from 35% to 99%. The capability has compounded while the interface has stayed frozen.

The result is a mismatch: 2025-era reasoning running inside 2022-era prompting conventions. Freeform strings talking to systems that could handle structured contracts. No verification because nobody specified what correct looks like. No typed steps because nobody wrote them down.

Ozma closes that gap. PLAN blocks replace open-ended instructions with typed, labeled, executable steps. CLAIM and INVARIANT blocks assert postconditions alongside edits. `§N` addressing lets the model reason about named positions rather than re-reading whole files. The session cache makes memory content-addressed and durable. Capability fingerprinting proves what the model can do at handshake rather than discovering it from failed output.

The shim is the migration path: intercepting existing agent frameworks, stripping implicit behavior, and making the contracts explicit. The native protocol model is the destination — no adapter layer, typed from the start.

The magic in LLM coding is stochastic, not conventional. Which is exactly why contracts matter more here, not less.
