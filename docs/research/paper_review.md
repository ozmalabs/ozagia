# Paper review: structured, balanced, multi-model

The paper-review workflow joins the graph pieces into one review:

```text
selected paper
  → claims, sources, references, checks
  → review plan (methods, statistics, domain, reproducibility, ethics, synthesis)
  → independent model runs
  → reconciliation with dissent
  → steelman / red-team / synthesis arguments per main claim
  → review.md, review.json, claims.csv, evidence.csv,
    references.bib, arguments.json, checks.json
```

It is designed to be thorough and balanced, not to produce a verdict. Findings,
arguments and recommendations are candidates and reviewer judgements.

## Starting a review

`research_paper_review_start` ingests a selected `.tex`, `.md` or `.markdown`
paper, creates a `ReviewPlan` targeting its `Source`, and returns a
deterministic review packet.

```python
started = service.call("paper_review_start", {
    "session_id": sid,
    "root_id": "papers",
    "path": "paper.tex",
    "question": "Is the treatment effective?",
    "roles": ["methods", "statistics", "domain", "reproducibility", "ethics"],
    "models": ["model-a", "model-b"],
    "min_reviewers": 2,
})
packet = started["packet"]
plan = started["plan"]
```

PDFs can be used directly: `paper_review_start` runs PDF claim extraction
(`pdf_claim_ingest`) and then creates the review plan and packet. PDF extraction
is lossy; claim candidates carry page/section locators, extraction confidence
and `math_degraded`. TeX/Markdown remains the stronger path for formulas and
formal statements.

## Packet

`research_paper_review_packet` collects:

- the paper Source, claims, evidence, assumptions and citation keys;
- reference checks and unresolved citations;
- extraction and appraisal tables for linked records;
- contradiction candidates and living-review alerts;
- the theory ledger and compatibility matrix;
- a deterministic review prompt and output schema.

The packet includes `packet_sha256` and `claim_ids`, so every run can record
exactly what it reviewed.

## Multi-model execution

`ozagia.harness.paper_review.run_paper_review` runs:

- the analysis roles through the existing review matrix;
- `advocate`, `critic` and `synthesizer` independently per selected claim;
- validation and recording of review findings and arguments;
- a consensus reconciliation when a graph is supplied.

```python
from ozagia.harness.paper_review import run_paper_review
from ozagia.harness.review import openai_runner

runner = openai_runner(base_url="https://host/v1", model="model-a",
                       api_key_env="OPENAI_API_KEY")
result = run_paper_review(
    plan=plan, packet=packet, model_runner=runner,
    graph=graph, record=True,
    bundle_dir=".ozagia/research/exports/review",
)
```

Argument outputs follow the `record_argument` schema: thesis, premises with
inference kinds and evidence/claim/citation links, assumptions, objections with
replies or explicit unanswered status, falsifiers and confidence. Invalid
output is reported and not recorded.

## Report

`research_paper_review_report` aggregates:

- the plan, models, runs and findings;
- summary, strengths, weaknesses, major/minor issues and questions;
- methods, statistics, reproducibility, ethics and related-work sections;
- claim/evidence mapping and reference checks;
- full steelman, red-team and synthesis arguments;
- theory competition, contradictions and living-review alerts;
- dissent and the complete audit trail.

Every section is present; empty sections say so explicitly. Recommendations and
confidence are shown only where a run or reconciliation recorded them.

## Bundle

`research_paper_review_bundle` writes:

- `review.md` — readable structured review;
- `review.json` — full machine-readable report;
- `claims.csv` — claim/evidence projection;
- `evidence.csv` — structured extraction table;
- `references.bib` — recorded bibliography;
- `arguments.json` — full steelman/red-team/synthesis records;
- `checks.json` — reference, contradiction, alert and review checks.

## Guardrails

- No truth verdict, automatic acceptance or rejection.
- Agreement is not accuracy; dissent is preserved.
- Steelman and red team are separate arguments, not one “balanced” paragraph.
- Model identity, role, prompt/context hashes and usage are recorded.
- API keys stay in the runner's environment/file and are never written to the
  bundle.
- A human reviewer remains responsible for the final judgement.
