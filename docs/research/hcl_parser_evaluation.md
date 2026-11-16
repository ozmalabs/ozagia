# Optional HCL parser evaluation — 2026-11-16

The Terraform/OpenTofu adapter keeps its bounded standard-library extractor.
This note records why no external HCL grammar was adopted yet, so the choice is
reviewable rather than implied by the partial parser.

## Candidate

`python-hcl2` 8.2.0 (MIT, Python >= 3.8, `lark>=1.1.5` and `regex`) was the
candidate: it is maintained, parses HCL2 including heredocs and expressions,
and reports malformed input with a line and column. A local `pip --dry-run`
resolved it without changing this repository.

## Findings

- The serialized Python object is convenient for declarations, but
  `hcl2.loads(..., serialization_options=SerializationOptions(with_meta=True))`
  emitted no `__start_line__`/`__end_line__` fields in 8.2.0, and the CLI
  `--with-meta` flag produced none either. Source spans would require a second
  scan or private tree APIs.
- The extractor contract needs stable source spans and deterministic keys for
  every declaration. Reconstructing spans from labels would weaken that
  contract and create a second, less tested location path.
- Parsing into dictionaries also materializes arbitrary attribute values in
  memory. The current extractor deliberately reads only identifiers and
  allowlisted structural paths, and never returns values.
- Malformed-input behavior is good for diagnostics (`UnexpectedToken` includes
  line/column), but the adapter already fails closed and reports an explicit
  per-file diagnostic. Adding a second parser only for diagnostics would double
  parse cost on every file unless gated to error paths.

## Decision

Do not add `python-hcl2` yet. Keep the stdlib extractor, which now masks
heredocs while preserving offsets, and document its remaining limits. Revisit
if a maintained grammar exposes stable spans through its public API, or if an
optional validation-only path is shown to improve diagnostics enough to justify
its cost and dependency surface. No target project or IaC tool was executed,
and no values from parsed HCL are stored in the graph.
