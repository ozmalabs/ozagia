# Deep review on a frozen HEAD archive (2026-11-18)

This is a bounded offline smoke run against a clean archive of source HEAD
`157d9183c36fbb8bbe5b82d0486af6cdd6e21ee7`. The target tree hash was
`f57bb019dccf38c9373bfb25227938f71311081a`, matching that source revision.
The archive was initialized and committed locally so git-dependent collectors
could run; the local commit was
`131da1df4cf12481d4be905b05ff63416c0005ad`. The review command used the
current working-tree Ozagia implementation because the deep-review code is
not present in source HEAD. No target code, tests, external scanners, network
requests or models were run.

## Reproduction

Run from the repository root, with `.venv` available:

```sh
mkdir -p /tmp/ozagia-head-157d9183
git archive --format=tar 157d9183c36fbb8bbe5b82d0486af6cdd6e21ee7 \
  | tar -xf - -C /tmp/ozagia-head-157d9183
git -C /tmp/ozagia-head-157d9183 init -q
git -C /tmp/ozagia-head-157d9183 config user.name 'Ozagia Eval'
git -C /tmp/ozagia-head-157d9183 config user.email 'eval@example.invalid'
git -C /tmp/ozagia-head-157d9183 add -A
git -C /tmp/ozagia-head-157d9183 commit -qm \
  'Frozen archive of upstream HEAD 157d9183'
PYTHONPATH=src .venv/bin/python -m ozagia.cli deep-review \
  --root /tmp/ozagia-head-157d9183 \
  --out /tmp/ozagia-head-review-20261118-git \
  --budget-s 60 --json \
  > /tmp/ozagia-head-review-cli-20261118-git.json
```

## Result

The command completed in 43.23 seconds against its 60 second cooperative
budget. It produced 468 findings: 17 major, 408 minor, 43 note and none
blocking. Evidence collection reached its 250-file cap and omitted 71 files.
The report is partial: the frozen checkout had no graph snapshot, correctness
has no collector, tests and line coverage were not run, and advisory status
remains unknown. No finding labels were created.

The first ten major findings were inspected by an automated coding agent as a
non-blinded spot check. Two were confirmed direct self-matches in the
scanner's own rule declarations (weak-password-hash and eval). The check also
surfaced a high-entropy candidate in an evaluation runner and intentional
installer/release examples; these are candidates for review, not findings
classified as false positives. This was not a human review and does not
estimate precision, recall or noise rate. Human labels and adjudication remain
outstanding.

## Comparison with the dirty checkout run

The prior dirty working-checkout smoke at the same source HEAD produced 519
findings (29 major, 444 minor, 46 note) in 43.67 seconds, with 117 evidence
files omitted. This frozen-tree run produced 51 fewer findings (12 fewer
major, 36 fewer minor and 3 fewer notes). These are not matched conditions:
the dirty run had a stale graph, while this archive had no graph. The difference
does not establish a quality improvement or a precision estimate. Both reports
are partial, and neither has human-labeled findings.

## Artifacts and reproducibility limits

The report and raw CLI output remain on the evaluation host under `/tmp`; they
are not checked in. SHA-256 digests from the frozen run:

- `manifest.json`: `2c93d76644d09e36d67a11d85c2915d765587542f1b164b90daf2c341ef7ba79`
- `findings.json`: `781e6cfc46346c293ccc41f736f079c248fc1d16efc1ee294d05b008b6d22808`

The note records the command, source identity, and aggregate metrics, but the
underlying report must be preserved separately to reproduce individual
findings. This smoke run does not satisfy the plan's clean realistic-corpus
precision/recall evaluation or its human-review requirement.

The blank top-ten human annotation sheet is
[`deep_review_frozen_head_annotation_sheet_2026-11-18.md`](deep_review_frozen_head_annotation_sheet_2026-11-18.md).
