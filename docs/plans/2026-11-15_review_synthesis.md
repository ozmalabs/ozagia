# Extraction, appraisal, contradiction and living review

**Date:** 2026-11-15. **Status:** stages A-C landed and integrated.

This closes the remaining synthesis gaps after the structured-review P0:
comparable extraction fields with effect sizes, risk-of-bias and certainty
grading, contradiction candidates, and living-review alerts. All records are
candidates; none establishes truth.

## Principles

- Extract fields, not prose. Every value carries a source locator or is marked
  as unavailable.
- Do not compute a pooled effect before units, measures and comparisons are
  comparable. This plan adds structured extraction and checks, not a
  meta-analysis engine.
- Risk-of-bias and certainty are reviewer judgements with recorded support.
- A contradiction is a candidate relation, not a verdict. Keep scope,
  direction, evidence and dissent visible.
- Living review reads recorded metadata only; provider refreshes stay explicit.
- Alerts are observations, not automatic edits or retractions.

## Graph types

Schema version 4 adds:

- `Extraction`: target, study design, population, intervention, comparator,
  outcomes, effect size, CI, p-value, sample size, unit, source locator,
  extractor, status.
- `RiskOfBias`: target, tool, domains, overall judgement, reviewer, notes.
- `Certainty`: target, framework, rating, reasons, downgrades/upgrades,
  reviewer.
- `Contradiction`: claims/evidence, kind, scope, rationale, status.
- `Alert`: kind, target, provider, detected time, payload, status.

Edges: `extracts`, `assesses`, `grades`, `contradicts`, `alerts`.

## Stage A: structured extraction (`research/extraction.py`)

API:

```python
record_extraction(graph, *, reference_id=None, claim_id=None, fields,
                  source_locator=None, extractor, reviewer=None)
check_extraction(state, extraction_id)
extraction_table(state, format="markdown"|"csv")
extraction_report(state, extraction_id)
```

Acceptance:

- Supports study design, population, intervention, comparator, outcomes,
  effect measure, effect size, CI bounds, p-value, sample size, unit, direction
  and notes.
- Rejects impossible values (negative sample size, CI low > high, p outside
  0..1, non-finite numbers) and unknown target IDs.
- `extraction_table` is deterministic and includes source locators.
- No meta-analysis or automatic pooling.

## Stage B: appraisal (`research/appraisal.py`)

API:

```python
record_risk_of_bias(graph, *, target_id, tool, domains, overall,
                    reviewer, notes=None)
check_risk_of_bias(state, assessment_id)
record_certainty(graph, *, target_id, rating, reasons, reviewer,
                 framework="GRADE", downgrades=None, upgrades=None)
certainty_report(state, target_id=None)
appraisal_table(state, format="markdown"|"csv")
```

Acceptance:

- Risk-of-bias tools include RoB 2, ROBINS-I and custom; judgements are
  low/some_concerns/high/unclear/not_applicable.
- Certainty ratings are high/moderate/low/very_low with reasons.
- Unknown targets and malformed domains are rejected.
- Reports preserve reviewer judgement and do not infer certainty automatically.

## Stage C: contradiction and living review

`research/contradiction.py`:

```python
find_contradictions(state, *, max_pairs=100)
record_contradiction(graph, *, claim_a_id, claim_b_id, kind, rationale,
                     evidence_ids=None, reviewer, status="candidate")
check_contradiction(state, contradiction_id)
contradiction_report(state)
```

Candidates come from deterministic signals: explicit `opposes` edges, claims
with the same scope but incompatible statuses, and extracted outcomes with the
same measure/unit/outcome where signs differ or confidence intervals do not
overlap. The detector does not use an LLM.

`research/living_review.py`:

```python
scan_alerts(state, *, review_plan_id=None, since=None, reference_ids=None)
record_alert(graph, *, kind, target_id, payload=None, reviewer=None,
             status="open")
acknowledge_alert(graph, alert_id, *, reviewer, note=None)
alert_report(state, *, status=None)
```

Alert kinds: `retracted_reference`, `expression_of_concern`,
`claim_cites_retracted`, `unresolved_citation`, `missing_identifier`,
`new_citation_since_plan`, `source_unavailable`. Scans read recorded graph
metadata only; provider fetch remains a separate explicit step.

## Integration

- Service operations and MCP schemas: extraction, appraisal, contradiction and
  alert operations.
- Docs: `docs/research/synthesis.md` plus `docs/research.md` links.
- Service-level tests with a fake extraction/appraisal/contradiction/alert
  workflow.
- Full `pytest` and `ruff` checks.

## Results

Landed 2026-11-15.

**A — extraction.** `research/extraction.py` records study design, population,
intervention, comparator, outcomes, measure, effect size, CI, p-value, sample
size, unit and direction with validation and source locators. Markdown/CSV
tables and checks. 20 tests passed.

**B — appraisal.** `research/appraisal.py` records RoB 2/ROBINS-I/custom
assessments and GRADE-like certainty with reasons and adjustments. Reports and
tables preserve reviewer judgement. 34 tests passed.

**C — contradiction and living review.** `research/contradiction.py` finds
explicit, status-based and extraction-based contradiction candidates and
records reviewed contradictions. `research/living_review.py` scans retractions,
expressions of concern, unresolved citations, missing identifiers, new citation
edges and unavailable sources, with record/acknowledge/report flows. 15 tests
passed.

**Integration.** Schema version 4 adds `Extraction`, `RiskOfBias`, `Certainty`,
`Contradiction` and `Alert` with `extracts`, `assesses`, `grades`,
`contradicts` and `alerts` edges. Service/MCP operations cover recording,
checking, tables/reports, contradiction scan/record and alert scan/record/ack.
A service-level test covers the whole extraction → appraisal → contradiction →
alert workflow.

**Checks.** `1116 passed, 37 skipped`; `ruff check src tests` passes.

## Non-goals

- No pooled effect sizes, meta-regression or heterogeneity statistics yet.
- No automatic risk-of-bias/certainty judgement.
- No LLM contradiction detector in the core.
- No automatic retraction of claims or references.
- No network calls in scans or tests.
