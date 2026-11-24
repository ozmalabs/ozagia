# Deep-review realistic corpus scaffolding and synthetic dry run (2026-11-24)

This is **scaffolding and a regression dry run, not realistic quality
evidence**. No public repository was acquired for evaluation, no human label was
entered, and no go/no-go gate was measured. The candidate corpus has 12
repositories and 50 change-range slots; every repository is marked
`candidate_unacquired` with null commit/tree/archive pins, and every slot is
`slot_unassigned` with null base/head commits. The dry run uses a synthetic
two-scope fixture with fictional reviewers and
`provenance: synthetic_test_fixture`.

## What was added

- `evaluation/deep_review_corpus/corpus.json`: versioned candidate manifest
  with repository identity/remote, license, language mix, size class, inclusion
  reason, holdout flag, local checkout expectation, and 50 range slots. No
  commits were invented.
- `evaluation/deep_review_corpus/templates/`: reviewer A, reviewer B and
  adjudicator templates.
- `evaluation/deep_review_corpus/README.md`: schema, acquisition, human
  labeling session, scoring and go/no-go runbook.
- `src/ozagia/eval/deep_review_corpus.py`: manifest validation, deterministic
  tree digest, offline checkout verification, and explicit-network archive
  acquisition. Downloads reuse the existing bounded GitHub archive/extraction
  code; no target code is imported or executed.
- `src/ozagia/eval/deep_review_corpus_labels.py`: label schema, duplicate-ID
  and reference checks, sealing and label-set validation.
- `src/ozagia/eval/deep_review_corpus_score.py`: deterministic per-scope,
  per-repository and per-subject scoring, labeled/unlabeled separation,
  coverage, digest checks, repeatability and proposed gate evaluation. It
  reuses `score_findings` and refuses to pool synthetic evidence into a
  realistic-corpus score.
- Fixture, tests and this note. The raw dry-run JSON is
  [`deep_review_corpus_dry_run_2026-11-24.json`](deep_review_corpus_dry_run_2026-11-24.json).

## Commands run

```sh
PYTHONPATH=src python3 -m ozagia.eval.deep_review_corpus validate

PYTHONPATH=src python3 -m ozagia.eval.deep_review_corpus_score \
  --manifest tests/fixtures/deep_review_corpus_dry_run/corpus.json \
  --labels tests/fixtures/deep_review_corpus_dry_run/labels \
  --record tests/fixtures/deep_review_corpus_dry_run/record.json \
  --checkout-root tests/fixtures/deep_review_corpus_dry_run \
  --out docs/results/deep_review_corpus_dry_run_2026-11-24.json

PYTHONPATH=src python3 -m pytest -q \
  tests/test_eval_deep_review_corpus.py \
  tests/test_eval_deep_review_corpus_labels.py \
  tests/test_eval_deep_review_corpus_score.py

ruff check src/ozagia/eval/deep_review_corpus.py \
  src/ozagia/eval/deep_review_corpus_labels.py \
  src/ozagia/eval/deep_review_corpus_score.py \
  tests/test_eval_deep_review_corpus.py \
  tests/test_eval_deep_review_corpus_labels.py \
  tests/test_eval_deep_review_corpus_score.py
```

Manifest validation reported 12 candidate repositories, 50 change-range slots,
0 assigned ranges, 2 holdout repositories, and language declarations spanning
Python, Rust, Go, JavaScript, TypeScript and C++. All 12 are unacquired.

The new focused test files passed (42 tests). Ruff reported no issues.

## Synthetic dry-run result

The fixture has one complete change-range scope and one partial scope. The
complete scope has four reviewed positive labels and one reviewed negative
label; the partial scope has one reviewed positive label. Predictions comprise
four findings in the complete scope and two in the partial scope.

| Measure | Raw values |
| --- | --- |
| Reviewed positive labels | 5 |
| Predicted `(path, type, subject)` pairs | 6 |
| True positives (all scopes) | 4 |
| True positives in complete scopes | 3 |
| False positives (complete scopes only) | 1 |
| False negatives | 1 |
| Complete-scope precision | 3 / 4 = 0.75 |
| Recall over reviewed positive labels | 4 / 5 = 0.80 |
| Report-noise ratio | (6 - 4) / 6 = 0.333 |
| Noise on complete scopes | 1 / 4 = 0.25 |
| Predictions in complete scopes / unlabeled scopes | 4 / 2 |
| High-impact (blocking+major) precision | 2 / 2 = 1.0 (two findings only) |
| Repeatability | identical finding IDs and content across the repeat |
| Digest checks | manifest, labels, reports and 6 source files verified |

The `overall.unlabeled` count is 1 (the unmatched prediction in the partial
scope), while `labeled_vs_unlabeled.unlabeled_predictions` is 2 because the
matched prediction in the partial scope is still unlabeled for precision
purposes. This is the intended distinction: partial-scope predictions are noise
context, not false positives and not verified true positives.

The scorer reported `status: synthetic_regression`, `quality_claim: none`,
`gate_status: not_applicable_synthetic`, and `pooling.synthetic_fixtures_included:
true`. These numbers are a pipeline regression check only.

## Limitations

- The fixture is hand-written synthetic data, not a public repository, not a
  real change range, and not a human judgment.
- No candidate repository has been acquired. The candidate licenses, language
  mixes and size classes are declarations, not offline-verified facts.
- No realistic human labels exist, so the realistic precision/recall/noise and
  go/no-go gates cannot be computed. A no-label realistic record would report
  unlabeled predictions and `insufficient_data`, not a pass.
- The dry run exercises digest pinning, repeat comparison and partial-scope
  handling, but not acquisition, checkout verification at a pinned commit, or
  adjudication of real reviewer disagreement.
- No network access was used; no target code was imported, executed or tested;
  no model was called.
- False-positive rate remains undefined because labels do not enumerate all
  actual negatives.

## What a human must do next

1. Verify each candidate's license and pin a real commit for at least 10
   repositories. Select and pin 50 public `base..head` ranges; do not guess.
2. Acquire and verify checkouts (tree/archive digests), then run the
   deterministic reviews into disposable report directories.
3. Have two reviewers label independently before seeing Ozagia findings, seal
   their files, and have a third person adjudicate. Use
   `--require-complete --require-human` only for the final frozen set.
4. Build a run record with report/manifest digests, completeness, graph
   freshness, timings and, separately, the blinded human-utility sample.
5. Run the scorer with `--require-realistic`; publish raw numerators,
   denominators, digests, exclusions and limitations. Do not treat a passing
   aggregate as universal correctness or as authorization for a blocking
   default.
