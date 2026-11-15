# Theory competition, compatibility and evidence basis

**Date:** 2026-11-15. **Status:** stages A-B landed and integrated.

A claim graph can show that evidence exists without showing which theory it
supports, whether two theories are rivals or compatible, whether they rest on
the same evidence, or whether the evidence can distinguish them. This plan adds
that theory-level layer. It remains descriptive: it does not decide which theory
is correct.

## Principles

- A theory is a structured candidate: statement, scope, mechanism, assumptions,
  predictions and status.
- Rivalry and compatibility are different relations. Two theories can be
  partial rivals and partially compatible, or compatible in one scope and not
  another.
- Compatibility is graded and recorded with a reviewer and rationale.
- Shared evidence is computed from explicit theory-evidence links, not guessed.
- “Insufficient to discern” is a first-class result, not a failure.
- Predictions and discriminating tests matter more than citation counts.
- No automatic theory choice and no LLM judgement in the core.

## Graph types

Schema version 5 adds `Theory` and these edges:

- `predicts`: Theory -> Prediction
- `rival_of`: Theory -> Theory
- `compatible_with`: Theory -> Theory (degree in edge provenance)
- `supported_by`: Theory -> Evidence/Claim/Observation/Extraction
- `challenged_by`: Theory -> Evidence/Claim/Observation/Extraction
- `discriminated_by`: Theory -> Prediction/Evidence/Observation
- `explains`: Theory -> Claim/Evidence/Observation

Existing `assumes` (Theory -> Assumption) and `tests` (Prediction ->
Evidence/Observation) are reused.

## Stage A: theory ledger (`research/theory.py`)

API:

```python
record_theory(graph, *, name, statement, scope, mechanism=None,
              assumptions=None, predictions=None,
              assumption_ids=None, prediction_ids=None,
              reviewer=None, status="proposed", notes=None)
record_rivalry(graph, theory_a, theory_b, *, scope=None,
               rationale=None, reviewer=None)
record_compatibility(graph, theory_a, theory_b, *, degree,
                     scope=None, rationale=None, reviewer=None,
                     evidence_ids=None)
link_theory_evidence(graph, theory_id, target_id, *,
                     relation="supported_by", rationale=None,
                     reviewer=None)
theory_compare(state, theory_a, theory_b)
theory_scan(state)
theory_report(state, theory_id=None)
compatibility_matrix(state)
```

Acceptance:

- Theory fields are validated; creations link assumptions and predictions.
- Rivalry is symmetric in reports; compatibility is undirected with degree
  `compatible`, `partially_compatible`, `incompatible` or `orthogonal`.
- Compatibility records include scope, rationale, reviewer and optional
  evidence IDs.
- `theory_compare` reports shared evidence, unique evidence, overlap ratio,
  predictions, discriminating evidence, compatibility records and evidence
  profiles (certainty/risk-of-bias/extraction counts). It returns
  `discernment.status` in `no_evidence`, `shared_only`,
  `unique_but_not_discriminating`, or `discriminating_evidence_present`.
- `theory_scan` reports theories without predictions, untested predictions,
  theories without evidence, rival pairs with only shared evidence, claims not
  explained by any theory, and contradictory compatibility/rivalry records.
- `theory_report` and `compatibility_matrix` are deterministic.
- No truth verdict; all outputs carry `truth_assessed=False`.

## Stage B: integration

- Service/MCP operations: `theory_record`, `theory_rivalry`,
  `theory_compatibility`, `theory_link_evidence`, `theory_compare`,
  `theory_scan`, `theory_report`, `theory_compatibility_matrix`.
- Docs: `docs/research/theory.md` and links from `docs/research.md`/README.
- Service-level test: two rival theories sharing one evidence item, one unique
  evidence item each, a partially compatible record, and a comparison that says
  the evidence is insufficient to discriminate.
- Full `pytest` and `ruff`.

## Results

Landed 2026-11-15.

**A — ledger.** `research/theory.py` records theories, assumptions and
predictions; rival and graded compatibility relations; theory-evidence links;
comparison with shared/unique evidence, overlap, evidence profiles and
discernment statuses; scans, reports and a compatibility matrix. 20 tests
passed.

**B — integration.** Schema version 5 adds `Theory` with `predicts`,
`rival_of`, `compatible_with`, `supported_by`, `challenged_by`,
`discriminated_by` and `explains` edges. Service/MCP operations cover theory
recording, rivalry, compatibility, evidence links, comparison, scan, report and
matrix. A service-level test covers two rival theories sharing one evidence
item, unique evidence for each, partial compatibility and a discriminating
test.

**Checks.** `1137 passed, 37 skipped`; `ruff check src tests` passes.

## Non-goals

- No automatic theory choice or probability.
- No LLM inference in the core.
- No claim that compatibility implies truth.
- No substitution of citation counts for discriminating evidence.
