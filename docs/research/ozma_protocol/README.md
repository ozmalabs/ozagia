# Ozma protocol research in Ozagia

Ozagia now maintains the Ozma protocol research as local project material. This is the starting point for designing Ozagia's model-facing interface and the connections among its graph, services, models and cognitive engines. The prior `ozma-ai` project is mothballed; understanding this material does not require a sibling checkout.

The imported protocol is a **design basis**, not an instruction to implement its draft unchanged. Ozagia can retain, adapt, replace or reject individual mechanisms as the design and evidence develop. The preserved text includes proposals, predictions and aspirational claims alongside implemented ideas; it is not a claim that Ozagia or the historical implementation already conforms to every part of the specification. New Ozagia decisions and tests should state what has actually been adopted.

## Start here

- [Foundation](upstream/docs/foundation.md): models as computational components and context as supplied input. Its strong claims about the causes of model failure are a position to evaluate, not an established result for every task.
- [Protocol specification v5.16](upstream/docs/protocol.md): draft model language, capabilities, work structures, grammar and proposed model–shim interaction.
- [Map format](upstream/docs/maps.md): typed, scoped, versioned and composable schemas and instances.
- [Shim requirements](upstream/docs/shim.md): the proposed runtime counterpart to model-facing semantics.
- [Primer](upstream/docs/protocol-primer.md): overview; some terminology and version examples reflect the time it was written.
- [Whole-surface partition](upstream/docs/protocol-partition.md): design for separating model-facing representation from host-side state and operations; not a claim that every proposed feature is implemented.
- [Changelog](upstream/docs/protocol-CHANGELOG.md), [reviews](upstream/docs/protocol-review-v5.1.md) and [protocol improvements](upstream/docs/protocol-improvements.md): prior design evolution and critiques.
- [Ozagia integration review](../../ozma_protocol_integration_review.md): comparison with existing Ozagia mechanisms and a proposed first integration path.

The [upstream documentation archive](upstream/docs/) retains the wider research context, including compression, serving, memory, evaluation, formal foundations, historical protocol reviews and experimental plans. Its original relative references to components outside the preserved subset may not resolve. The [historical protocol source](upstream/ozmalib/protocol/) and [selected tests](upstream/tests/) are preserved for inspection and possible porting: they are **not** installed or imported by Ozagia and are not a self-contained, tested Ozagia runtime. In particular, the original code may depend on modules, resources or setup outside the copied subset.

## Working direction

The useful organizing idea is to separate the responsibilities that together make model work dependable:

1. **Host-side state and capability:** Ozagia can hold project/research graphs, tasks, policy, tools, runtime state and evidence. Each subsystem retains ownership of its own facts.
2. **Model-facing representation:** select and version a small task-relevant view or map, including source, scope, freshness, trust and omissions. Select an encoding and fallback the actual model and harness can use.
3. **Returned proposals:** parse and validate against the appropriate representation, resolve references against the right snapshot, and pass actions through Ozagia's permission, preview and transaction checks.
4. **Verification and feedback:** record actual inputs, results and verification level; distinguish assertions, static observations, executed tests and formally checked derivations. Update the relevant state only when the operation and evidence support it.
5. **Cognitive integration:** Ocelot is a separate cognitive engine, including scientific reasoning. The protocol is a candidate interface for connecting its capabilities, not a claim that those capabilities have already been integrated or that all conclusions are correct.

Capability negotiation must not grant authority. Model output must not establish its own permissions or verify itself. Labels and instructions alone do not prove isolation from prompt injection; host-side boundaries and adversarial testing remain necessary. Use graceful fallback, explicit unknown states, and measured end-to-end performance instead of assuming that protocol encoding or additional scaffolding improves every task.

## Provenance and integrity

- Imported from `ozma-ai/research` in the local `ozma-ai` repository at commit **`33a39c8`**, 2026-11-17. The `research/` subtree was clean when copied; the parent repository had unrelated uncommitted backend changes.
- Original license: [MIT, copyright 2026 Ozma Labs Pty Ltd](LICENSE). The imported materials retain that license; Ozagia's root license covers its own material.
- [SHA-256 manifest](upstream/MANIFEST.sha256) records the copied upstream files. From `upstream/`, run `sha256sum -c MANIFEST.sha256` to verify the preserved snapshot.
- This archive preserves the historical source verbatim. New project-level design and adoption decisions belong outside `upstream/`; changes to the preserved snapshot should be intentional and attributed.
