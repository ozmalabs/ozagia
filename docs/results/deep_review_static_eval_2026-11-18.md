# Deep review static pattern evaluation

Reproduce from the repository root with:

```sh
.venv/bin/python -m ozagia.eval.deep_review_static \
  --out docs/results/deep_review_static_eval_2026-11-18.json
```

The frozen corpus contains four reviewed positive examples and eight reviewed
near misses, one positive for each currently measured rule. The near misses
exercise different iterables in nested loops, an exception that is logged and
re-raised, an immutable function default, and streamed `range` iteration.
`labels.json` treats every empty-label case as negative for the four measured
rules. Each per-rule negative count includes all other cases, including the
positive examples for different rules.

This run produced 4 true positives, 0 false positives and 0 false negatives:
precision 1.0, recall 1.0, finding noise rate 0.0, and negative-case false
alarm rate 0.0 (0 of 8). Each rule had 1 true positive and 11 true negatives,
with 0 false positives. The deterministic JSON includes a SHA-256 digest of
the labeled source corpus and per-case predictions. It intentionally omits
timing so repeated runs are byte-for-byte comparable.

These values describe a small, hand-reviewed synthetic corpus. They are useful
as a regression check for obvious pattern noise, not as an estimate of
production precision or recall. A reviewer judgment that a case is a negative
is specific to these candidate rules and examples.
