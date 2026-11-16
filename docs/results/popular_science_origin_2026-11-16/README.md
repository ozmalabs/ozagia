# Popular-science demo: Darwin's *Origin* with three models

This is a real, end-to-end run of the research workflow against a public-domain
popular-science book excerpt. It is both a demonstration and a reproducible
integration test target.

- **Work:** Charles Darwin, *On the Origin of Species* (1859), excerpt from the
  Introduction, "Difficulties on Theory" and the closing "entangled bank"
  passage. Public domain; source text from Project Gutenberg eBook 1228.
- **Fixture:** [`tests/fixtures/research/origin_excerpt.md`](../../../tests/fixtures/research/origin_excerpt.md)
- **Run script:** [`examples/popular_science_review.py`](../../../examples/popular_science_review.py)
- **Offline test:** [`tests/test_popular_science_demo.py`](../../../tests/test_popular_science_demo.py)

## Models

| Role in the spread | Model |
|---|---|
| Cloud, fast | DeepSeek V4.1 Flash 1017 (`opencode-go/deepseek-v4.1-flash`) |
| Cloud, general | GPT-6 Luna (`opencode-go/gpt-6-luna`) |
| Local, 27B | Qwen3.8-27B (`vllm/qwen3.8-27b`) |

Each model ran independently through the OpenCode CLI. The models did not see
each other's output before reconciliation. No API key is stored in this bundle.

## What the workflow did

1. Ingested the excerpt as a **book**, not as a paper or note.
2. Recorded the default holding: `interpretation`, editorial authority,
   `treat_as_author_argument`, `not_as_behavioural_evidence_without_extraction`.
3. Extracted two candidate claims, both from Darwin's first-person conclusion:
   - species are not immutable and genera descend from generally extinct
     ancestors;
   - natural selection is the main but not exclusive means of modification.
4. Built a review plan and packet with the book Source, both claims, the book
   Reference, reference checks and theory context.
5. Ran a `domain` review independently on each model.
6. Ran `advocate` (steelman) and `critic` (red-team) arguments independently on
   the first claim for each model.
7. Reconciled the review findings with exact clustering, then with **Ocelot
   graph-shape/analogy search**, and wrote the structured bundle.

## Result

- 3 independent review runs, 12 findings after validation and 9 recorded
  strengths.
- 6 independent arguments: 3 steelmen and 3 red-team cases.
- 18 exact issue clusters; 0 exact consensus, 18 dissent clusters.
- Ocelot structural reconciliation: 2 analogies, 16 clusters, **2 consensus**
  and 14 dissent clusters.
- Ocelot merged duplicate-shape pairs (`duplicate_claim` / `claim_overlap`) and
  evidence-shape pairs (`evidence_scope` / `missing_evidence_link`) without
  merging different issue families or different claim targets.
- 18,770 input tokens and 4,585 output tokens across the review runs, as
  reported by the CLI.

The exact-string view is intentionally conservative: all three models largely
agree on the substance, but word the issues differently, so exact clustering
finds no consensus. Ocelot's graph-shape search adds a second view that uses the
target, issue family and structural relations rather than wording. It is a
candidate reconciliation; the exact clusters and dissent remain recorded.

Model emphases:

- **DeepSeek V4.1 Flash:** 5 findings, 3 strengths. Duplicate claim, compound
  claim, interpretation versus support, domain accuracy, and proof hygiene.
- **GPT-6 Luna:** 3 findings, 2 strengths. Missing evidence scope, duplicate
  claim scope, and provenance.
- **Qwen3.8-27B:** 4 findings, 4 strengths. Duplication, missing evidence,
  falsifiability and reference identification.

One earlier DeepSeek red-team attempt was rejected because it cited
`kimura-1968-neutral-theory`, a reference absent from the packet. That is the
validation boundary working: only packet records and resolving citation keys can
become arguments. The final run recorded all six arguments.

## Arguments

The steelmen and red-team cases are not one-paragraph summaries. Each contains:

- a thesis and confidence;
- premises with inference kinds (`deductive`, `inductive`, `abductive`,
  `analogy`);
- assumptions;
- objections, including replies or explicit unresolved status;
- falsifiers.

See `bundle/arguments.json` for the full records and `bundle/review.md` for the
readable report.

## Ocelot reconciliation

The Ocelot pass is optional and external. The demo worker mirrors findings into
one `Microtheory` per model, one `Concept` per finding, and structural edges for
the target and issue family. It loads Ocelot's `shape_vocabulary` and
`analogy_search` seeds and calls the graph analogy search. The worker lives at
`examples/ocelot_reconcile_worker.py`; ozagia core imports Ocelot only if the
optional in-process bridge is selected.

Ocelot is MIT licensed. It is not bundled with ozagia: the demo uses the sibling
`../ocelot` checkout when available and falls back to exact reconciliation
otherwise. In-process mode uses the checkout's `substrate_rs` runtime; subprocess
mode runs the worker in the Ocelot venv.

## Bundle

| File | Contents |
|---|---|
| `bundle/review.md` | Readable structured review |
| `bundle/review.json` | Full machine-readable report |
| `bundle/claims.csv` | Claim/evidence projection |
| `bundle/evidence.csv` | Structured extraction table |
| `bundle/references.bib` | Recorded bibliography |
| `bundle/arguments.json` | Full steelman/red-team/synthesis arguments |
| `bundle/checks.json` | Reference, contradiction, alert and review checks |
| `demo.json` | Run summary, models, holdings, reconciliation and usage |
| `runs.json` | Raw driver result, including model outputs and validation |

## Reproduce

Offline (fake models, no network or model calls):

```bash
.venv/bin/python -m pytest -q tests/test_popular_science_demo.py
```

Live (requires OpenCode model access and explicit approval):

```bash
OZAGIA_EVAL_LIVE=1 .venv/bin/python examples/popular_science_review.py --live
```

Override the model list with repeated `--model` flags. Use `--no-ocelot` to skip
the optional structural reconciliation.

## Limits

This is a demonstration on a short excerpt, not a full review of the book. The
extractor found only the explicit first-person thesis statements; the supporting
chapters were not in the fixture. Ocelot's structural clustering is a candidate
view, and exact-clustering dissent remains visible. The output records candidates
and reviewer judgements, never truth.
