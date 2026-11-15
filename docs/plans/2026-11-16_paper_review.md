# Paper review: complete structured review with steelman and red team

**Date:** 2026-11-16. **Status:** stages A-B landed and integrated.

The review pieces are now separate: paper ingestion and claims, references,
extraction, appraisal, theory competition, contradictions, living review,
multi-model review, reconciliation and arguments. This plan joins them into one
paper-level workflow:

```text
paper
  → claims, evidence, references, checks
  → review plan (analysis roles + advocate/critic/synthesizer)
  → independent model runs
  → reconciliation with dissent
  → steelman / red-team / synthesis arguments per main claim
  → structured review report and bundle
```

The output should be a thorough, balanced review: strengths and weaknesses,
methods/statistics/reproducibility/ethics findings, claim-evidence mapping,
theory competition, contradictions, living-review alerts, steelman and red-team
arguments, dissent and a final reviewer judgement. Judgement-based sections are
labelled as such; no model output becomes truth.

## Principles

- The report contains model findings and reviewer judgements, not verdicts.
- Steelman and red team are separate arguments, each with premises, evidence,
  assumptions, objections, replies or unresolved status, and falsifiers.
- Reconciliation preserves dissent.
- A complete review can say “the evidence is insufficient to distinguish these
  theories.”
- The core builds packets and reports; the harness runs models.
- All model runs record model, role, prompt/context/output hashes and usage.

## Stage A: paper review report (`research/paper_review.py`)

API:

```python
start_paper_review(graph, *, root_id, path, question, roles, models, ...)
build_paper_review_packet(state, *, source_id, plan_id=None,
                          manuscript_body=None, max_claims=50)
paper_review_report(state, *, plan_id)
write_paper_review_bundle(graph, *, plan_id, output_dir=None)
```

Packet contents:

- source and paper metadata;
- claims linked to the source, evidence, assumptions, citation contexts;
- reference checks, extraction and appraisal tables;
- contradiction candidates and living-review alerts;
- theory ledger/compare context;
- a deterministic review prompt and output schema;
- `packet_sha256` and `truth_assessed=False`.

Report contents:

- paper/source, plan, roles/models, runs and findings;
- summary, strengths, weaknesses, issues by severity, questions;
- methods, statistics, reproducibility, ethics and related-work sections;
- claims/evidence table and unresolved citations;
- steelman/red-team/synthesis arguments with full premise/objection details;
- theory competition and compatibility;
- contradictions, alerts and dissent;
- recommendation/confidence only as recorded, never inferred;
- complete audit trail.

Bundle files: `review.md`, `review.json`, `claims.csv`, `evidence.csv`,
`references.bib`, `arguments.json`, `checks.json`.

Acceptance:

- A source with claims produces a deterministic packet.
- The report renders Markdown and JSON with every section, including empty
  sections explicitly marked.
- The bundle writes only the expected files and defaults under the research
  state directory.
- `start_paper_review` ingests a selected TeX/Markdown paper, creates a
  `ReviewPlan` targeting its Source and returns the packet.

## Stage B: paper review driver (`harness/paper_review.py`)

API:

```python
run_paper_review(*, plan, packet, model_runner,
                 review_roles=None, argument_roles=None, models=None,
                 claim_ids=None, graph=None, record=True,
                 max_workers=4, bundle_dir=None)
```

Acceptance:

- Review roles run through the existing `harness.review.run_review_matrix`.
- Argument roles (`advocate`, `critic`, `synthesizer`) run independently per
  selected claim; each returns the `record_argument` schema.
- Invalid argument output is reported and not recorded.
- With `graph` and `record=True`, review runs/findings, arguments and a
  reconciliation are stored.
- `bundle_dir` writes the paper review bundle through
  `research.paper_review.write_paper_review_bundle`.
- Tests use fake model runners only; no live calls.
- CLI is optional and, if present, live-gated by `--live` and
  `OZAGIA_EVAL_LIVE=1`.

## Integration

- Service/MCP operations: `paper_review_start`, `paper_review_packet`,
  `paper_review_report`, `paper_review_bundle`.
- Docs: `docs/research/paper_review.md` plus links.
- Service-level test: a small TeX paper becomes a packet, fake review and
  argument runs are recorded, a reconciliation is produced, and the bundle
  contains the expected files.
- Full `pytest` and `ruff`.

## Results

Landed 2026-11-16.

**A — packet and report.** `research/paper_review.py` starts a review from a
selected TeX/Markdown paper, builds a paper-level packet (claims, evidence,
references, extraction, appraisal, contradictions, alerts, theory context),
aggregates a structured report and writes the seven-file bundle. 9 tests
passed.

**B — driver.** `harness/paper_review.py` runs analysis roles through the review
matrix, runs advocate/critic/synthesizer independently per claim, validates and
records arguments, reconciles findings, and optionally writes the bundle. 6
tests passed.

**Integration.** `Source` is a review target. Service/MCP operations:
`paper_review_start`, `paper_review_packet`, `paper_review_report`,
`paper_review_bundle`. A service-level test covers ingest → plan → review runs →
steelman argument → reconciliation → report → bundle.

**Checks.** `1200 passed, 37 skipped`; `ruff check src tests` passes.

## Non-goals

- No automatic acceptance/rejection or truth verdict.
- No claim extraction from PDFs in this stage.
- No hidden model calls in the core.
- No deletion of minority findings.
- No meta-analysis.
