# Corpus labels

No human labels exist yet. This directory intentionally contains no label
files. Reviewer and adjudicator templates are in `../templates/`, and blank
reviewer packets for the acquired repositories/ranges are in the external
output root (`<checkout-root>/out/packets/`).

Add files as `labels/<repository-id>/<subject-key>/reviewer-a.json`,
`reviewer-b.json` and `adjudication.json` only after a human review session,
then seal and validate them with `ozagia.eval.deep_review_corpus_labels` (see
`../README.md`). A blank template copied here is not a label.

The dry-run fixture under `tests/fixtures/deep_review_corpus_dry_run/` is
synthetic test data. It must never be copied here or scored as realistic
evidence.
