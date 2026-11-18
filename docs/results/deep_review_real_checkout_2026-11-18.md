# Deep review on the working OzAGIa checkout (2026-11-18)

This is a bounded, offline smoke evaluation of the Phase 4 report pipeline on
the current checkout. It is a realistic-corpus observation, not a clean or
representative benchmark: at run time the checkout had extensive staged,
unstaged and untracked changes, including the deep-review implementation being
evaluated. HEAD was `157d9183c36fbb8bbe5b82d0486af6cdd6e21ee7`. No tests,
external scanners, network requests or model calls were run. The graph snapshot
already in `.ozagia/` was stale; it was not refreshed.

Reproduction (report output kept outside the checkout):

```sh
.venv/bin/python -m ozagia.cli deep-review --root . \
  --out /tmp/ozagia-real-review-20261118 --budget-s 60 --json
```

The command returned 0 and wrote a report in 43.67 seconds against its 60
second cooperative budget. It reported 519 findings: 29 major, 444 minor and
46 note; none blocking. Evidence collection reached its 250-file cap and
omitted 117 evidence files. All selected sections were partial or unknown;
correctness had no collector. The stale graph skipped cycle, hub, layer,
large-function and symbol coverage checks. Tests and line coverage were not
run, dependency advisories were not queried, source contents were not scanned
for secrets, and several fixture/example trees were intentionally excluded.
The structured report and raw evidence remain in `/tmp/ozagia-real-review-20261118/`
on the evaluation host; they are not checked into this repository.

## Agent spot check

An automated coding agent inspected the first ten major findings in the generated priority order. At
least four are clear scanner self-matches rather than actionable target risks:

- `weak-password-hash` matches the review engine's own regex/rule declaration
  in `src/ozagia/core/security.py`.
- `unsafe-yaml` and `shell=True` match the dangerous-token inventory in
  `src/ozagia/core/code_review.py`.
- `generic_high_entropy` matches the documentation example of the
  `secret://` reference scheme in `src/ozagia/core/secrets.py`.

The release workflow and installer examples that intentionally document
`curl | bash` also appear as major findings; these need rule/context review
before they can be counted as true positives. This ten-item, non-blinded
inspection is not a precision estimate. It does show that scanning the tool's
own implementation and documentation currently produces prominent false
positives. The report correctly labels findings as static evidence and records
limitations, but the current priority list is too noisy for unreviewed use as a
go/no-go signal.

The follow-up scanner filter ignores Python comments and docstrings, plus
Python rule/token/pattern registry assignments only when their values are
literal constants and containers. Calls and computed values in registry-named
assignments remain scanned. Generic secret matching also rejects known
`secret://` reference examples and dotted identifier expressions. Focused
regression tests preserve findings in executable code. This is a narrow
context filter: it does not classify every example embedded in arbitrary
string constants, templates or generated files, and it may miss a real secret
deliberately written in a comment or docstring.
It does not replace human review or establish repository-wide precision.

## Result and limits

This run verifies that the full report can be produced within a one-minute
budget on the working checkout and that stale graph evidence is surfaced as
partial coverage. It does not measure recall, overall precision, memory, or
change-review quality. Because the target was dirty and included active
changes, the findings cannot be attributed to a stable release. A useful next
evaluation needs a frozen clean revision, human-reviewed labels for a
sample, and preferably a scan exclusion/context policy for analyzer rules,
examples and docs before comparing whole-repo and diff results.
