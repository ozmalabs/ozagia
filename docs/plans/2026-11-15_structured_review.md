# Structured review, steelman and multi-model reconciliation

**Date:** 2026-11-15. **Status:** P0 stages A-C landed and integrated.

This plan adds the judgment layer missing after the reference/acquisition and
deferred research work: a review protocol, independent model runs, validated
findings, reconciliation with preserved dissent, and a steelman/red-team
argument object. Models are reviewers, not truth.

## Principles

- Independent first: each model/role gets the same deterministic packet but no
  other model's output. Record prompt, context and output hashes.
- Agreement is not accuracy. Report kappa/Jaccard and dissent; never turn a
  majority vote into a `supports` edge.
- The core never calls a model. A separate driver fans out; the service records
  and checks results.
- Every finding is a candidate. Citation keys, claim IDs and quotes are checked
  mechanically before they enter the review report.
- A steelman is an argument with obligations: premises, assumptions, inference,
  objections, replies and falsifiers. It is not an endorsement.
- Human adjudication remains available and is recorded.

## Graph types

Added in schema version 3:

- `ReviewPlan`: question, kind, roles, models, criteria, sources, rounds,
  blinding, minimum reviewers, adjudication policy.
- `ReviewRun`: plan, reviewer, role, model, provider, round, packet/prompt/
  context/output hashes, params, tokens, cost, status.
- `ReviewFinding`: severity, issue type, target, quote, citation keys,
  recommendation, confidence.
- `Reconciliation`: method, clusters, consensus, dissent, metrics, adjudicator.
- `Argument`: mode, thesis, premises, assumptions, objections, replies,
  falsifiers, confidence, reviewer.
- `SearchRun`: plan, query, provider, filters, counts, export hash.

Edges: `addresses`, `reviews`, `finds`, `about`, `reconciles`, `agrees_with`,
`disagrees_with`, `implements`, `searches`, `argues_for`, `uses`.

## Stage A: review plan and reconciliation (`research/structured_review.py`)

API:

```python
create_review_plan(graph, *, question, kind, roles, models, ...)
record_search_run(graph, plan_id, *, query, provider, ...)
build_review_packet(state, *, plan_id=None, claim_id=None,
                    reference_id=None, manuscript_body=None)
validate_review_output(output, *, state=None, packet=None)
record_review_run(graph, *, plan_id, reviewer_id, role, model,
                  packet_sha256, output, ...)
review_agreement(state, plan_id)
record_reconciliation(graph, *, plan_id, method, ...)
review_report(state, plan_id)
```

Acceptance:

- Packet is deterministic and includes graph context, checks and prompt.
- Invalid schema, unknown IDs/citation keys and unsupported quotes are
  reported rather than recorded as valid.
- Runs link to plans and findings; findings link to targets.
- Agreement reports Fleiss' kappa for finding presence, mean pairwise Jaccard,
  severity agreement, consensus and dissent.
- Reconciliation preserves minority findings and links to its runs.

## Stage B: steelman/red-team (`research/argument.py`)

API:

```python
build_argument_brief(state, *, claim_id, mode="steelman")
record_argument(graph, *, claim_id, mode, thesis, premises, assumptions,
                objections, falsifiers, confidence, reviewer, ...)
check_argument(state, argument_id)
```

Acceptance:

- Brief gathers supports, opposes, assumptions, evidence, citation contexts and
  gaps; it returns a prompt and packet hash without calling a model.
- Premises must have evidence, a claim link, citation keys, or an explicit
  `inference="assumption"`.
- Objections have responses/status; falsifiers are reported.
- Recorded arguments link `argues_for` the claim and `uses` evidence/claims/
  references; no `supports` edges are created.
- `check_argument` reports gaps mechanically.

## Stage C: multi-model driver (`harness/review.py`)

API:

```python
run_review_matrix(plan, packet, *, model_runner,
                  roles=None, models=None, round="independent",
                  max_workers=4, graph=None, record=False)
openai_runner(*, base_url, api_key_env, ...)
```

Acceptance:

- Builds role-specific prompts and runs each role × model independently.
- `model_runner` is injectable; tests never call a live model.
- Outputs are validated through `structured_review.validate_review_output`.
- Records runs/findings only when a graph and `record=True` are supplied.
- Usage, model/provider and prompt/context hashes are retained; API keys are
  never logged.
- Parallel execution returns results in deterministic role/model order.

## Integration

- Service operations: `review_plan_create`, `review_search_log`,
  `review_packet`, `review_validate`, `review_run_record`,
  `review_agreement`, `review_reconcile`, `review_report`,
  `argument_brief`, `argument_record`, `argument_check`.
- MCP schemas and CLI choices.
- Tests at service level with fake model outputs.
- Docs: `docs/research.md` and `docs/research/review.md`.

## Results

Landed 2026-11-15.

**A — review spine.** `research/structured_review.py` records plans, search
runs, deterministic packets, validated model outputs, independent runs and
findings, agreement metrics, reconciliations and reports. 7 tests passed.

**B — argument object.** `research/argument.py` builds steelman/red-team/
synthesis briefs, records candidate `Argument` nodes with evidence obligations,
and checks premise/citation/objection/falsifier gaps. 9 tests passed.

**C — multi-model driver.** `harness/review.py` runs a role × model matrix with
injectable runners, validates outputs, records runs/findings only on request,
and provides an OpenAI-compatible runner plus a live-gated CLI. 15 tests passed.

**Integration.** Schema version 3 adds `ReviewPlan`, `ReviewRun`,
`ReviewFinding`, `Reconciliation`, `Argument` and `SearchRun` with review
edges. Service/MCP operations added for plan creation, search logs, packets,
validation, run recording, agreement, reconciliation, reports, and argument
brief/record/check/report. A service-level test covers two independent runs,
consensus/dissent, reconciliation and a steelman argument.

**Checks.** `1046 passed, 37 skipped`; `ruff check src tests` passes.

## Non-goals

- No automatic acceptance, rejection or truth verdict.
- No majority vote that creates support evidence.
- No bundled model provider credentials or automatic external calls.
- No silent deletion of dissent.
- No claim extraction from PDFs.
