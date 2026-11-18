# Frozen HEAD finding annotation sheet (2026-11-18)

This is a blank sheet for independent human review of the first ten major
findings, in the priority order emitted by the frozen-HEAD report. Each ID
links to its finding in the report output on the evaluation host. The report
and reproduction details are described in
[`deep_review_frozen_head_smoke_2026-11-18.md`](deep_review_frozen_head_smoke_2026-11-18.md).

## Review status

No human labels or approvals have been entered. The earlier automated agent
spot check is separate from this sheet and is not a human judgment or ground
truth. Reviewers should fill the label columns independently before discussing
findings or seeing another reviewer's labels. The report links point to
`/tmp/ozagia-head-review-20261118-git/` and are available only on the
evaluation host that ran the review.

For each item, reviewers should record whether the claim is correct, whether
the severity is appropriate, whether evidence supports the claim, and whether
the recommendation is actionable. Use `yes`, `no`, or `uncertain`; explain
uncertainty or disagreement in notes. Keep these fields blank until a human
review is performed.

## Findings

| # | Finding | Location | Sanitized claim | Correct? | Severity appropriate? | Evidence sufficient? | Actionable? | Reviewer / date | Notes |
| ---: | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 1 | [dr-07fa4c235dc33ceb](/tmp/ozagia-head-review-20261118-git/01-security.md#finding-dr-07fa4c235dc33ceb) | `src/ozagia/core/security.py:509, 511, 513` | Password hash rule pattern matched |  |  |  |  |  |  |
| 2 | [dr-40bb452eeac142aa](/tmp/ozagia-head-review-20261118-git/01-security.md#finding-dr-40bb452eeac142aa) | `src/ozagia/selfinstall.py:234` | Installer command pipes downloaded content to a shell |  |  |  |  |  |  |
| 3 | [dr-52909b0304ef0c43](/tmp/ozagia-head-review-20261118-git/01-security.md#finding-dr-52909b0304ef0c43) | `evaluation/m6_live/run_m6.py:872` | High-entropy secret candidate; value withheld |  |  |  |  |  |  |
| 4 | [dr-54242d291ad8c77e](/tmp/ozagia-head-review-20261118-git/01-security.md#finding-dr-54242d291ad8c77e) | `src/ozagia/cli.py:198` | `eval()` call may execute untrusted input |  |  |  |  |  |  |
| 5 | [dr-6b2c212e39e5076f](/tmp/ozagia-head-review-20261118-git/01-security.md#finding-dr-6b2c212e39e5076f) | `src/ozagia/core/security.py:358` | `eval()` rule pattern matched |  |  |  |  |  |  |
| 6 | [dr-765a62612c00836f](/tmp/ozagia-head-review-20261118-git/01-security.md#finding-dr-765a62612c00836f) | `scripts/release/notes.py:36, 38` | Release note example pipes downloaded content to a shell |  |  |  |  |  |  |
| 7 | [dr-889cfc504d9675be](/tmp/ozagia-head-review-20261118-git/01-security.md#finding-dr-889cfc504d9675be) | `src/ozagia/core/pulumi_graph.py:128` | YAML load is flagged for unsafe deserialization |  |  |  |  |  |  |
| 8 | [dr-918dc8b322071df7](/tmp/ozagia-head-review-20261118-git/01-security.md#finding-dr-918dc8b322071df7) | `src/ozagia/edit/yaml_edit.py:85` | YAML load is flagged for unsafe deserialization |  |  |  |  |  |  |
| 9 | [dr-b74d71ddc765fa39](/tmp/ozagia-head-review-20261118-git/01-security.md#finding-dr-b74d71ddc765fa39) | `src/ozagia/core/openapi.py:72` | YAML load is flagged for unsafe deserialization |  |  |  |  |  |  |
| 10 | [dr-bf1d96bf5c7ee803](/tmp/ozagia-head-review-20261118-git/01-security.md#finding-dr-bf1d96bf5c7ee803) | `src/ozagia/harness/native.py:568` | High-entropy secret candidate; value withheld |  |  |  |  |  |  |

This is a stratified convenience sample of the report's top major findings,
not a random sample of all findings. Its ratings cannot establish overall
precision, recall, or report-noise rate.
