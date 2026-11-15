# Extraction, appraisal, contradictions and living review

This layer sits between the review plan and the manuscript. It records what a
source reports, how much a reviewer trusts it, where claims conflict, and what
has changed since a review was planned. All records are candidates; none is a
truth verdict.

## Structured extraction

`research_extraction_record` records a small, comparable schema for a
`Reference` or `Claim`:

```text
study_design, population, intervention, comparator, outcomes,
effect_measure, effect_size, ci_low, ci_high, p_value,
sample_size, unit, direction, notes
```

The extractor must supply the target, a source locator and an extractor id.
Numeric fields must be finite; sample size must be a positive integer; p-values
must be in `0..1`; CI bounds must be ordered. Unknown fields are rejected so
tables stay comparable.

`research_extraction_check` reports missing design/outcome/measure/locator,
inconsistent numbers, and missing graph links. `research_extraction_table`
exports Markdown or CSV. This is not a meta-analysis: effects are not converted,
pooled or weighted.

## Risk of bias and certainty

`research_risk_of_bias_record` records a reviewer assessment against a
Reference, Claim or Extraction. Tools include RoB 2, ROBINS-I and custom.
Domains carry a judgement (`low`, `some_concerns`, `high`, `unclear`,
`not_applicable`) and support or a source locator.

`research_certainty_record` records a GRADE-like rating (`high`, `moderate`,
`low`, `very_low`), reasons, and optional downgrade/upgrade items. The framework
is recorded. `research_certainty_report` reports ratings, risk-of-bias
summaries and warnings such as high certainty alongside high risk of bias.

Neither operation infers a judgement. They record what a reviewer decided.

## Contradictions

`research_contradiction_scan` is deterministic and read-only. It looks for:

- explicit `opposes` edges between Claims, Evidence or Observations;
- Claims with the same normalized scope and incompatible statuses;
- Extractions for the same outcome/measure/unit whose effect signs differ or
  whose confidence intervals do not overlap.

`research_contradiction_record` records a reviewed `Contradiction` with both
claims, kind, rationale, evidence and status (`candidate`, `confirmed`,
`dismissed`). `research_contradiction_check` verifies links, and
`research_contradiction_report` counts candidates and records.

A contradiction is not a verdict between two papers. It is a flagged relation
for review.

## Living review

`research_alert_scan` reads recorded metadata only:

- retracted references and expressions of concern;
- claims citing retracted references;
- unresolved citation keys;
- references with no identifier;
- `cites_work` edges retrieved after the review plan was created or after a
  supplied timestamp;
- sources explicitly marked unavailable/stale.

`research_alert_record` records an alert against a Reference, Claim, Source or
ReviewPlan. `research_alert_ack` acknowledges it with a reviewer and note.
`research_alert_report` counts open/acknowledged/dismissed alerts.

Scanning does not call a provider or change the graph. A provider refresh
(citation graph, retraction check) is a separate explicit step.

## Limits

- No meta-analysis, unit conversion or pooled effect sizes.
- No automatic risk-of-bias or certainty judgement.
- No LLM contradiction detector in the core.
- No automatic retraction of claims or references.
- Alerts are observations; a person decides what to do.
