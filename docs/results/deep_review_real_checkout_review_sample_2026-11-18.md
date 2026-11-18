# Human review sample: deep-review working-checkout findings

This is an unreviewed annotation sheet for the first ten entries under
“Top risks” in the 2026-11-18 working-checkout report. It contains the report's
finding IDs, claims and locations; it does not contain secret values or source
excerpts. No reviewer labels have been filled in.

The updated post-filter run and its current sample are in
[`deep_review_real_checkout_context_comparison_2026-11-18.md`](deep_review_real_checkout_context_comparison_2026-11-18.md).

The source run reviewed the dirty checkout at revision
`157d9183c36fbb8bbe5b82d0486af6cdd6e21ee7`. Its full report was written to
`/tmp/ozagia-real-review-20261118/` on the evaluation host. The scanner context
filter was changed after that run, so treat these IDs and line numbers as a
historical sample from that report, not as findings verified against the
current working tree. See
[`deep_review_real_checkout_2026-11-18.md`](deep_review_real_checkout_2026-11-18.md)
for methods and limits.

For each row, select one neutral label and add a short reason. Suggested labels:
**supported concern**, **false positive**, **needs context**, or **cannot
determine**. These labels are for reviewer assessment; they are not implied by
the generated severity or confidence.

| Rank | Finding ID | Report severity / confidence | Report claim | Report locations | Reviewer label | Reason / evidence |
| ---: | --- | --- | --- | --- | --- | --- |
| 1 | `dr-07fa4c235dc33ceb` | major / medium | A password appears to use MD5/SHA-1 | `src/ozagia/core/security.py:509`, `:511`, `:513` |  |  |
| 2 | `dr-139466f38d9b89b1` | major / high | A download is piped directly into a shell | `.github/workflows/release.yml:8` |  |  |
| 3 | `dr-3aa4ed23134f3a4c` | major / medium | YAML is loaded without a safe loader | `src/ozagia/core/code_review.py:137` |  |  |
| 4 | `dr-40bb452eeac142aa` | major / high | A download is piped directly into a shell | `src/ozagia/selfinstall.py:234` |  |  |
| 5 | `dr-4f7fd27f2d649eba` | major / medium | Secret-like value matched `generic_high_entropy`; value withheld | `src/ozagia/core/secrets.py:12`, `:14`, `:15`, `:1692`, `:1924` |  |  |
| 6 | `dr-52909b0304ef0c43` | major / medium | Secret-like value matched `generic_high_entropy`; value withheld | `evaluation/m6_live/run_m6.py:872` |  |  |
| 7 | `dr-54242d291ad8c77e` | major / medium | `eval()` can execute attacker-controlled input | `src/ozagia/cli.py:215` |  |  |
| 8 | `dr-6b2c212e39e5076f` | major / medium | `eval()` can execute attacker-controlled input | `src/ozagia/core/security.py:358` |  |  |
| 9 | `dr-765a62612c00836f` | major / high | A download is piped directly into a shell | `scripts/release/notes.py:36`, `:38` |  |  |
| 10 | `dr-773df67ccb9d2a6c` | major / high | Subprocess call enables a shell | `src/ozagia/core/code_review.py:134` |  |  |

This purposive sample follows the report's displayed risk order. It is not a
random sample and cannot estimate overall precision or recall. Reviewer
agreement, adjudication and a reviewed-label score have not been collected.
