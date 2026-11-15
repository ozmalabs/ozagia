# Theory competition and evidence basis

The theory ledger records what a claim graph usually leaves implicit: which
theory a claim belongs to, what that theory assumes and predicts, which theories
are rivals or partially compatible, and whether the evidence can actually tell
them apart.

It is descriptive. It never chooses a winner or turns compatibility into truth.

## Theory records

`research_theory_record` creates a `Theory` with:

- name, statement, scope and optional mechanism;
- assumptions (new `Assumption` nodes or existing IDs);
- predictions (new `Prediction` nodes or existing IDs);
- reviewer, status (`proposed`, `active`, `rival`, `superseded`) and notes.

`research_theory_link_evidence` links a theory to:

- `supported_by` — Evidence, Claim, Observation or Extraction;
- `challenged_by` — the same;
- `discriminated_by` — a Prediction, Evidence or Observation that can separate
  it from a rival;
- `explains` — a Claim, Evidence or Observation.

Existing `assumes` and `tests` edges connect assumptions and predictions to
their observations.

## Rivals and compatibility

`research_theory_rivalry` records a rival relation with scope and rationale.
Rivalry is treated as symmetric in reports.

`research_theory_compatibility` records an undirected, graded relation:

- `compatible`
- `partially_compatible`
- `incompatible`
- `orthogonal`

Each record carries scope, rationale, reviewer and optional evidence IDs. A
pair can be rivals in one scope and partially compatible in another; the
scope is part of the record.

## Comparing theories

`research_theory_compare` reports:

- rivalry and compatibility records, including degree and scope;
- supported and challenged evidence for each theory;
- shared evidence, unique evidence and overlap ratio;
- predictions and the tests/observations attached to them;
- `discriminated_by` targets;
- evidence profiles: counts by target type, certainty ratings, risk-of-bias
  judgements and extraction study designs;
- a discernment status.

Discernment statuses:

- `no_evidence` — neither theory has recorded evidence.
- `shared_only` — both rest on the same evidence and there is no unique item.
- `unique_but_not_discriminating` — each has different evidence, but none is
  linked as discriminating and no prediction/test separates them.
- `discriminating_evidence_present` — at least one explicit discriminating
  target or unique discriminating test is recorded.

“Different evidence” is not the same as “evidence that distinguishes.” That is
the point of the third status.

## Scans and reports

`research_theory_scan` flags:

- theories without predictions;
- predictions with no tests;
- theories without evidence;
- rival pairs whose evidence is shared-only or not discriminating;
- claims not explained by any theory;
- pairs recorded as both rivals and compatible/partially compatible.

`research_theory_report` returns one theory or the whole ledger with checks.
`research_theory_compatibility_matrix` returns a deterministic pair matrix with
shared/unique evidence counts and an explicit `missing` marker where no
compatibility relation is recorded.

## Limits

- No automatic theory choice, probability or truth verdict.
- No LLM inference in the core; models can propose records through the review
  workflow.
- Compatibility is a reviewer judgement, not a logical proof.
- Citation counts are not evidence levels.
- A theory with no recorded evidence may still be worth testing; the scan
  reports gaps, not failure.
