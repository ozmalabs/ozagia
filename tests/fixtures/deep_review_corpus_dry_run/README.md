# Synthetic dry-run corpus (not quality evidence)

This directory is a hand-written synthetic fixture for the corpus manifest,
label and scoring pipeline. It is not a public repository, not a real change
range, and not a human judgment. Reviewer ids, timestamps and labels are
fictional test data with `provenance: "synthetic_test_fixture"`.

- `corpus.json`: one synthetic repository and two change-range slots. Both slots
  are `slot_unassigned` with null commits; no commit is invented.
- `labels/fixture-service/range-complete/`: two reviewers and an adjudicator
  declare a complete scope.
- `labels/fixture-service/range-partial/`: the same files declare a partial
  scope so precision stays undefined and predictions stay unlabeled.
- `reports/`: two `findings.json` artifacts plus a repeat copy.
- `record.json`: pins manifest, labels and report digests and records a repeat.
- `checkout/fixture-service/`: source files used only to verify label
  source-file digests. They are never imported or executed.

The scorer must report `status: synthetic_regression`,
`gate_status: not_applicable_synthetic` and `quality_claim: none` for this
fixture. Never copy these files into `evaluation/deep_review_corpus/labels/`.
