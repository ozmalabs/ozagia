# Deep-review context-filter comparison and current review sample

Offline rerun on the working checkout after the scanner context-filter changes.
The output is in `/tmp/ozagia-real-review-post-context-20261118/` on the
evaluation host. The run used the same Git HEAD as the earlier report
(`157d9183c36fbb8bbe5b82d0486af6cdd6e21ee7`) and the same full profile,
60-second budget and stale graph snapshot. It did not refresh the graph, run
tests or external scanners, make network requests, or call models.

## Before and after

| Measure | Earlier run | Context-filter rerun | Change |
| --- | ---: | ---: | ---: |
| Total findings | 519 | 508 | -11 |
| Major findings | 29 | 17 | -12 |
| Security findings | 259 | 248 | -11 |
| Minor findings | 444 | 445 | +1 |
| Note findings | 46 | 46 | 0 |
| Elapsed time | 43.67 s | 47.47 s | +3.80 s |
| Evidence files recorded / cap | 250 / 250 | 250 / 250 | unchanged |
| Evidence files omitted | 117 | 106 | -11 |

Twelve earlier finding IDs no longer appear: `dr-139466f38d9b89b1`,
`dr-3aa4ed23134f3a4c`, `dr-4f7fd27f2d649eba`, `dr-773df67ccb9d2a6c`,
`dr-93ddf18246306854`, `dr-adf51db2bec3cde8`, `dr-b3ba3e0ac2e87b18`,
`dr-cecf993f5ad4c37d`, `dr-cf529550ebae8275`, `dr-e4214ba097e84881`,
`dr-f420801d147038ee`, and `dr-f648807aadf5cb55`. They were detections in
comment/docstring text or literal scanner rule/token declarations. One finding
was added (`dr-bc16cfbb963f12b3`, minor, SSRF candidate in
`src/ozagia/core/deep_review/verification.py:66-67`). The working tree changed
between runs, so this count comparison is descriptive, not a controlled
estimate of the filter's causal effect.

Both runs were partial and non-exhaustive. The existing graph snapshot was
stale relative to HEAD, so graph-dependent checks were skipped. The rerun
reached the 250-file evidence cap, omitted 106 evidence files and completed in
47.47 seconds within the cooperative budget. At rerun time `git status
--porcelain` listed 102 changed paths. This dirty tree includes ongoing work;
findings cannot be attributed to a release or immutable source tree. See the
[initial run notes](deep_review_real_checkout_2026-11-18.md) for other scope
limits.

## Current annotation sample

The rows below are the first ten “Top risks” in the rerun README. They are
unreviewed. Choose one label per row: **supported concern**, **false positive**,
**needs context**, or **cannot determine**. The generated severity and
confidence are reproduced as report metadata, not reviewer conclusions.

| Rank | Finding ID | Report severity / confidence | Report claim | Current report location(s) | Reviewer label | Reason / evidence |
| ---: | --- | --- | --- | --- | --- | --- |
| 1 | `dr-07fa4c235dc33ceb` | major / medium | A password appears to use MD5/SHA-1 | `src/ozagia/core/security.py:600`, `:602`, `:604` |  |  |
| 2 | `dr-40bb452eeac142aa` | major / high | A download is piped directly into a shell | `src/ozagia/selfinstall.py:234` |  |  |
| 3 | `dr-52909b0304ef0c43` | major / medium | Secret-like value matched `generic_high_entropy`; value withheld | `evaluation/m6_live/run_m6.py:872` |  |  |
| 4 | `dr-54242d291ad8c77e` | major / medium | `eval()` can execute attacker-controlled input | `src/ozagia/cli.py:218` |  |  |
| 5 | `dr-6b2c212e39e5076f` | major / medium | `eval()` can execute attacker-controlled input | `src/ozagia/core/security.py:449` |  |  |
| 6 | `dr-765a62612c00836f` | major / high | A download is piped directly into a shell | `scripts/release/notes.py:36`, `:38` |  |  |
| 7 | `dr-889cfc504d9675be` | major / medium | YAML is loaded without a safe loader | `src/ozagia/core/pulumi_graph.py:128` |  |  |
| 8 | `dr-918dc8b322071df7` | major / medium | YAML is loaded without a safe loader | `src/ozagia/edit/yaml_edit.py:85` |  |  |
| 9 | `dr-b74d71ddc765fa39` | major / medium | YAML is loaded without a safe loader | `src/ozagia/core/openapi.py:72` |  |  |
| 10 | `dr-bf1d96bf5c7ee803` | major / medium | Secret-like value matched `generic_high_entropy`; value withheld | `src/ozagia/harness/native.py:568` |  |  |

No reviewer labels have been filled in. This priority-selected sample is not
random; it cannot estimate precision or recall. No reviewer agreement,
adjudication or scored labels are available yet.
