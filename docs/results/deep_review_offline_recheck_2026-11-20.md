# Deep review offline fixture recheck (2026-11-20)

This is a synthetic regression recheck at source revision `f8f91f8`. It is
not a realistic-corpus evaluation or a human quality assessment, and does not
validate the proposed rollout thresholds. The fixture, labels and evaluation
code are in this repository. Results are one local run; timing and allocation
measurements vary by host.

Commands, run from the repository root with its development environment:

```sh
.venv/bin/python -m ozagia.eval.deep_review_static
.venv/bin/python -m ozagia.eval.deep_review_report
.venv/bin/python -m ozagia.eval.deep_review_comparison --comparison all
```

- The static fixture reported 4 true positives, 0 false positives and 0 false
  negatives across 12 cases (four positives and eight near misses). These
  figures cover only its four seeded Python rules.
- The service report fixture produced eight findings in each of two runs,
  with stable IDs. Its reviewed labels expanded to eight scored path/type/
  subject pairs: eight matched, with no false positives or false negatives in
  its declared complete subjects. Ten subjects were partial and one unknown.
  The first report took approximately 0.92 seconds and contained 70,237 bytes;
  the Python allocation peak was approximately 2.4 MB, not process RSS.
- The synthetic range condition matched both of its two change labels; the
  whole-repository condition matched neither of those range-specific labels.
  The range comparison was partial and is not evidence of general recall.
- The graph-less condition had eight matches among eight labeled pairs. The
  graph-aware condition also matched eight labels and added four unlabeled
  pairs among twelve scored pairs; its reported noise ratio was one third.
  The fixture has no inter-function call edges, so graph-added candidates are
  partly a fixture artifact.
- The scripted model comparison reported three advisory candidates, no change
  to deterministic findings, zero live-provider calls and no network use.
  It reported one added noise candidate. This does not evaluate a live model.

For report-label comparisons, conventional false-positive rate is unknown
because labels do not enumerate actual negatives. The static fixture's
near-miss cases provide a separate, limited negative set. Durations and peak
memory are single-host samples; `tracemalloc` excludes native allocations and
process RSS is not isolated per report. No target code or tests were executed
by the reviews. A realistic frozen corpus with independently adjudicated human
labels is still needed for quality and rollout decisions.
