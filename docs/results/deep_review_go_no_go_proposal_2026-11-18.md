# Deep review go/no-go proposal (2026-11-18)

**Proposal only. These thresholds and procedures have not been validated and
are not rollout results. No new corpus was evaluated for this document.** The
only measured results remain the synthetic fixture results in
[`deep_review_phase4_2026-11-18.md`](deep_review_phase4_2026-11-18.md), which
explicitly do not estimate production quality.

This proposal gives reviewers a concrete bar to accept, revise, or reject
before Ozagia considers changing any default behavior. It does not enable a
blocking default. `--fail-on` remains caller-selected; the default remains
`never` regardless of whether the proposal is later accepted.

## Proposed evaluation corpus

Use a frozen, documented set of at least 10 realistic repositories and 50
change ranges, with multiple repository sizes and at least three language
families supported by the relevant collectors. Include both active and
maintenance repositories, tests, package/configuration files, and changes
with known security, correctness, resilience, performance, and documentation
issues. Record repository revision, collector coverage, language mix, file and
graph sizes, and the reason for inclusion. Keep a separate holdout set that is
not used to tune rules.

For every evaluated change, two reviewers independently label findings in the
scanned diff and directly affected code. Labels must include subject, type,
severity, location or scope, supporting evidence, and whether the report's
wording is actionable. Reviewers should not see Ozagia findings before their
independent pass. A third reviewer adjudicates disagreements. Preserve the
label file and its digest with the evaluation record; do not include private
repository contents in public artifacts without authorization.

Fixtures remain useful for rule regression and near-miss tests, but fixture
scores are reported separately from realistic-corpus scores. Do not pool them
into one headline score.

## Proposed go/no-go thresholds

Compute metrics on the holdout set and report per-subject results as well as
the aggregate. A subject counts as labeled only when reviewers completed its
declared scope; unlabeled predictions are reported as coverage/noise context,
not silently treated as true or false positives.

| Gate | Proposed go threshold | No-go condition |
| --- | --- | --- |
| Deterministic report quality | Overall precision at least 0.80 and recall at least 0.70 on labeled holdout subjects | Either threshold missed, or fewer than 8 of 10 repositories have complete labels |
| High-impact findings | At least 0.90 precision for blocking/major findings; every blocking finding has directly re-readable deterministic evidence and reviewer agreement | Any unsupported blocking finding, or high-impact precision below 0.90 |
| Subject stability | No evaluated subject with at least 20 predictions has precision below 0.65; always publish subject-level counts | Any such subject falls below 0.65 without an explicit profile exclusion or rule correction |
| Noise | Report-noise ratio at most 0.20 on fully labeled scope; publish unlabeled prediction count separately | Noise exceeds 0.20 or is not measurable because labels are incomplete |
| Repeatability and coverage | Identical frozen inputs reproduce deterministic IDs and findings; stale/missing graph and unavailable tools are visibly partial/unknown | Any stale graph silently produces a clean graph-dependent subject, or deterministic repeat differs without an explained input change |
| Operational bounds | At least 95% of offline runs finish within the documented per-run budget and do not write outside the report/state paths | Budget failures are hidden, output escapes the allowed paths, or the failure rate exceeds 5% |
| Human utility | At least 80% of a blinded top-finding sample is judged actionable by both independent reviewers or adjudicator | Actionability below 80%, or severity is routinely overstated |

These are proposed minimums, not measured guarantees. Report raw numerators and
denominators with every rate. Do not use a passing aggregate to hide a weak
subject or an unreviewed blocking finding. A no-go means revise, narrow the
profile, or collect more evidence; it does not authorize a hidden behavior
change.

## Proposed human review sample

Review at least 100 findings, or all findings if the corpus produces fewer
than 100. Sample across repositories and subjects, stratifying for severity,
confidence, graph-dependent findings, and findings added by optional evidence.
Include every blocking candidate and a random sample of major, minor, note,
and no-evidence/unknown cases. Keep the random seed and sampled finding IDs in
the private evaluation record. Two reviewers independently rate correctness,
evidence sufficiency, severity, actionability, and whether the report clearly
states limitations; adjudicate disagreements. Publish aggregate ratings and
counts, not private source excerpts.

## Reproducible protocol

1. Freeze the code revision, Python version, dependency lock, collector
   configuration, corpus repository revisions, and label digests. Start with
   a clean copy of each checkout and record its commit and tree digest.
2. Run the existing synthetic regression commands separately:

   ```sh
   .venv/bin/python -m ozagia.eval.deep_review_static
   .venv/bin/python -m ozagia.eval.deep_review_report
   .venv/bin/python -m ozagia.eval.deep_review_comparison --comparison all
   ```

   Preserve their raw JSON output. These are regression checks, not realistic
   quality evidence.
3. For each realistic revision, run a deterministic whole-repository report
   and a change-range report into a disposable output directory. Use an
   explicit `--out`, `--range BASE..HEAD`, and fixed `--budget-s`; leave
   `--model-pass` and `--run-bench` off. Run the graph-aware condition only
   with a fresh graph and the graph-less condition on a separate clean copy.
   Record manifest, findings, timings, peak memory, report bytes, and exit
   status. Never fetch or execute target code as part of this protocol.
4. Repeat each deterministic condition twice from equivalent clean checkouts.
   Compare stable finding IDs, subjects, locations, evidence references and
   report hashes, accounting only for manifest timestamps/durations. Keep any
   mismatch and its input identity in the record.
5. Score each condition against the independently adjudicated labels using
   precision, recall, false-positive rate, report-noise ratio and change-set
   new/resolved/unchanged IDs. Report confusion counts, complete-scope counts,
   skipped/unknown coverage and per-repository/per-subject distributions.
6. Draw and review the human sample described above. Keep blinded ratings,
   adjudications, random seed, sampled IDs and summary calculations.
7. If optional model review is separately evaluated, use a fake runner first
   to prove deterministic findings remain unchanged. Any live-provider study
   is a separate, explicitly approved experiment and is not part of the
   offline acceptance protocol. Model suggestions remain advisory and cannot
   affect default exit behavior.
8. Publish the exact commands, source revision, corpus manifest, label-file
   digests, raw evaluator outputs, aggregation script/version, exclusions,
   metric definitions, resource records, human-review method, and all failed
   or partial runs. Redact private repository identities where required while
   preserving reproducible public fixtures for any released examples.

## Rollout decision

Passing the proposed gates would support keeping the capability available and
may justify narrower, caller-selected CI use. It would not establish universal
correctness and would not change the default from non-blocking. Any proposal
to enable a blocking default requires a separate decision, a larger blind
evaluation, calibrated per-subject gates, and review of the false-positive
cost in the intended deployment context.
