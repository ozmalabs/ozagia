# Structured review, steelman and multi-model reconciliation

The review layer turns graph context into a reviewable process: a plan, an
independent run per reviewer/model, validated findings, agreement statistics
and a reconciliation that preserves dissent. The core never calls a model; a
separate driver does that and records what came back.

## Review plan

`research_review_plan_create` records a `ReviewPlan`: question, kind
(`systematic`, `rapid`, `narrative`, `peer_review`, `steelman`), roles, models,
criteria, sources, rounds, blinding, minimum reviewers and adjudication policy.
A plan can address a Claim, Reference, Report or Argument. Search queries are
recorded separately as `SearchRun` nodes.

```text
ReviewPlan
  ├─ SearchRun[]       query, provider, filters, counts, export hash
  ├─ ReviewRun[]       one role × model × round attempt
  │    └─ ReviewFinding[]
  └─ Reconciliation[]  consensus, dissent, metrics, adjudicator
```

## Independent runs

Each role × model combination gets the same deterministic packet but no other
reviewer's output. A run records:

- reviewer id, role, model, provider, round;
- packet, prompt and context hashes;
- output hash, summary, recommendation and confidence;
- parameters, tokens and cost;
- its findings and their claim/reference/evidence targets.

The first round should be blind. Cross-review is a later, explicitly labelled
round; otherwise “independent” is not true.

## Validation

Before findings are recorded:

- the output must match the review schema;
- severity must be `blocking`, `major`, `minor` or `note`;
- claim/reference/evidence IDs must exist;
- citation keys must resolve to `Reference` nodes;
- a quote must occur in the supplied packet.

Invalid output is reported and not recorded. These checks are mechanical; they
do not judge whether a finding is correct.

## Agreement and reconciliation

`research_review_agreement` reports:

- Fleiss' kappa for binary presence of each finding cluster across runs;
- mean pairwise Jaccard similarity of finding sets;
- severity agreement where two or more reviewers found the same issue;
- consensus findings (at least the plan's minimum reviewers);
- dissent (a finding only one reviewer raised).

`research_review_reconcile` records a `Reconciliation` with the metrics,
clusters, consensus and dissent, and links back to every run. Methods include
`consensus`, `adjudicated` and `minority`; the method and any adjudicator are
recorded. Reconciliation never deletes dissent or creates `supports` edges.

Agreement is a property of reviewers, not evidence about the world. Five
models sharing a prompt and context can agree and all be wrong.

## Steelman / red team

`research_argument_brief` gathers the strongest available material around a
claim: supporting and opposing evidence, assumptions, derived claims, citation
contexts, references, unresolved keys and gaps. It returns a deterministic
prompt and packet hash without calling a model.

`research_argument_record` records a candidate `Argument`:

- thesis and mode (`steelman`, `red_team`, `synthesis`);
- premises with inference kind and evidence/claim/citation links;
- assumptions;
- objections with replies or an explicit unanswered status;
- falsifiers, confidence and reviewer.

`research_argument_check` verifies that non-assumption premises have support,
IDs and citations resolve, objections have replies or explicit unresolved
status, falsifiers are present, and the argument did not create support or
opposition edges. An argument is a candidate, never an endorsement.

## Multi-model driver

`ozagia.harness.review` runs the matrix outside the core:

```python
from ozagia.harness.review import openai_runner, run_review_matrix

runner = openai_runner(
    base_url="https://host/v1",
    model="model-name",
    api_key_env="OPENAI_API_KEY",
)
result = run_review_matrix(
    plan, packet, model_runner=runner, graph=graph, record=True,
)
```

`run_review_matrix` is also usable with any injected `model_runner`, which is
how tests avoid live calls. It runs roles × models with a thread pool, validates
each output, and records runs/findings only when a graph and `record=True` are
supplied. Results are returned in deterministic role/model order.

The CLI is live-gated:

```bash
OZAGIA_EVAL_LIVE=1 python -m ozagia.harness.review --live \
  --plan plan.json --packet packet.json \
  --base-url https://host/v1 --models model-a,model-b \
  --roles methods,statistics --output runs.json
```

API keys are read from an environment variable or file and only sent in the
Authorization header. They are not placed in prompts, outputs or logs.

## Guardrails

- Paper text is untrusted. A model can be prompt-injected; validation and quote
  checks matter more, not less, with several models.
- Record model version, provider, prompt/context hashes, temperature, seed,
  tokens and cost for every run.
- Decide whether a manuscript may leave the machine; support local models and
  provider allowlists.
- Keep a human adjudication path for contested findings.
- Calibrate agreement against human labels before treating it as quality.
- Do not turn a majority vote into support evidence.
