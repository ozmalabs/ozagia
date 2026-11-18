# Deep review Phase 4 evaluation (2026-11-18)

Synthetic-fixture evaluation of the offline deep-review report pipeline,
reviewed-label scoring and the range, model-pass and graph comparison harnesses.
Reproduce from the repository root:

```sh
.venv/bin/python -m ozagia.eval.deep_review_report
.venv/bin/python -m ozagia.eval.deep_review_comparison --comparison all
```

Both commands print JSON by default and accept `--out PATH`. The report fixture
is `tests/fixtures/deep_review_service`; its labels are in
`tests/fixtures/deep_review_service/labels.json`. Complete report-label subjects
are `performance`, `failure-modes`, `data-structures`, `scalability` and
`tests-and-coverage` (eight expected labels: four case labels and four report
findings). The range comparison uses
`tests/fixtures/deep_review_range/labels.json`, with two expected range-added
labels in `src/checkout/gateway.py` for security and test coverage. A curated
machine-readable summary is in
[deep_review_phase4_2026-11-18.json](deep_review_phase4_2026-11-18.json); it is
not raw evaluator output.

All fixtures are synthetic. No target module is imported or executed, no tests
are run, and the model-pass comparison uses only a scripted fake runner; no live
provider or network is involved.

## Report evaluator

Two full reports were generated and scored against the reviewed labels, then
compared by stable finding ID.

| Measure | Result |
| --- | ---: |
| Findings / expected labels (per report) | 8 / 8 |
| True positive / false positive / false negative (per report) | 8 / 0 / 0 |
| Precision / recall | 1.0 / 1.0 |
| False-positive rate / report-noise ratio | 0.0 / 0.0 |
| Reports / report files / report bytes | 2 / 16 / 88,802 |
| Total duration | 1.47 s |
| tracemalloc peak | 11,054,753 B |
| Process RSS high-water | 34,402,304 B |

The report file count and byte total cover both reports. Process RSS is the
process-lifetime high-water mark, not an isolated per-report measurement. The
earlier repeatability-only record
`docs/results/deep_review_report_fixture_2026-11-18.json` predates
reviewed-label scoring and is not the source of these metrics.

## Range comparison

A whole-repo report was compared with the same report plus a deterministic
two-commit range.

| Measure | Result |
| --- | --- |
| Whole-repo findings / range findings | 10 / 12 |
| New / resolved / unchanged stable IDs | 2 / 0 / 10 |
| Range change-set precision / recall | 1.0 / 1.0 |
| Range change-set false-positive rate / noise | 0.0 / 0.0 |
| Whole-repo change-set recall on range-only labels | 0.0 |
| Range graph freshness | missing |
| Commit determinism | deterministic base and head commits |

The whole-repo report cannot emit range-only rule identities, so its zero
change-set recall on range-only labels is an identity/scoring limitation, not a
claim that whole-repo review found nothing.

## Scripted model-pass comparison

The deterministic report was compared with an injected fake runner. This
exercises the candidate-validation and scoring seam, not model quality.

| Measure | Result |
| --- | --- |
| Baseline deterministic precision / recall | 1.0 / 1.0 |
| Accepted / deduplicated / unique / supported candidates | 6 / 3 / 3 / 2 |
| Candidate precision / recall | 0.667 / 0.25 |
| Added noise / added-noise rate | 1 / 0.333 |
| Duplicate rate | 0.5 |
| Usage | 200 prompt + 40 completion tokens across 2 runs |
| Deterministic findings unchanged | yes |
| `live_provider_calls` / `network` / `truth_assessed` | 0 / false / false |

## Graph comparison

An absent-graph report was compared with an `index=True` report on an identical
copy of the fixture.

| Measure | Graph-less | Graph-aware |
| --- | ---: | ---: |
| Findings | 8 | 16 |
| Labeled precision / recall (complete subjects) | 1.0 / 1.0 | 1.0 / 1.0 |
| Precision over all predictions | 1.0 | 0.667 |
| Report-noise ratio | 0.0 | 0.333 (4 unlabeled graph-added pairs) |
| Priority graph freshness | error | fresh (17 nodes / 32 edges) |
| Findings with direct symbol evidence | 0 | 4 |

Graph-aware review added 8 finding IDs, removed 0 and shared 8 with the
graph-less report. Both copies used the same commit hashes.

The fixture has no inter-function call edges, so graph-added
unreferenced-symbol candidates are partly a fixture artifact. The extra
unlabeled pairs are measured noise, not evidence that graph-aware review
improves review quality.

## Limits

- These are single-host samples. Duration and memory vary by host and load;
  tracemalloc excludes native allocations, and process RSS is not isolated to
  one report.
- The fixtures and labels are small and hand-seeded. The scores are regression
  evidence, not estimates of production precision, recall or noise.
- The model-pass comparison uses a scripted runner and therefore says nothing
  about live model precision or recall. Deterministic findings remained
  unchanged and model output remained unverified.
- The range comparison measures this fixture's change-set labels only. It does
  not establish range review recall on a real repository.
- These results do not close Phase 4. Realistic-corpus comparisons, human
  review of top findings and published go/no-go thresholds remain.
