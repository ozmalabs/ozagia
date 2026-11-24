# Deep-review noise audit (2026-11-25)

This is an **internal noise audit of the acquired deep-review corpus**, not a
quality score and not an adjudication. No human label exists, no finding was
scored against ground truth, and no go/no-go gate is measured. It uses the 62
deterministic reports (`--profile full`, no graph, no tests, no linters) to
identify which rules and collectors produce the most output and which output is
least likely to be actionable, then fixes only high-confidence, generic false
positives and duplicate-emission defects.

- Corpus: `ozagia-deep-review-realistic-v1`,
  `/home/matt/ozma/deep-review-corpus`
- Reports audited: 62 (12 whole-repo + 50 ranges) before regeneration
- Findings audited: 3,407 report entries, 920 distinct `(repository, id)` findings
- Holdouts: `pytest` and `gin`. Their findings are counted in aggregates only;
  no rule, threshold or convention change below was justified from them.
- Result: 6 generic rule/collector fixes in the first pass (3,407 -> 3,234
  report entries), then 2 follow-up fixes on the same day (3,234 -> 3,218):
  189 fewer report entries in total. Corpus reports and reviewer packets were
  rebuilt after both passes; the follow-up is recorded below.
- Manifest pins and digests are unchanged: `corpus.json` is untouched and its
  digest remains
  `5fae870cbb0d83b55c3904da97408a01f30af4cb116df072de9ec9120c1598c0`.

## Method

1. Read every `findings.json` and grouped entries by subject, type, severity,
   confidence, repository and a normalized title signature.
2. Separated **report entries** from **distinct observations**. A whole-repo
   finding is repeated in each range report for that repository, so raw counts
   measure report volume, not unique defects. Distinct counts below use
   `(repository_id, finding_id)`; within one report IDs are unique, but 14 IDs
   collide across repositories because `finding_id` does not include the
   repository (see "Remaining noise").
3. Sampled high-volume categories (bounded to a few files per category,
   non-holdout only) against the pinned read-only checkouts in
   `/home/matt/ozma/deep-review-corpus/repositories/<id>`.
4. Classified each sampled finding only as **clear false positive**,
   **intentional/known limitation**, **duplicate/noise**, or **plausibly
   actionable**. No item was labeled true/false positive for a precision score.
5. Fixed only patterns with a generic cause and a regression test that keeps a
   true-positive case. Ambiguous patterns are reported, not changed.

## Aggregate result

### Reports before regeneration

| Subject | Report entries | Distinct (repo, id) |
| --- | ---: | ---: |
| tests-and-coverage | 945 | 376 |
| dependencies | 732 | 131 |
| security | 609 | 124 |
| architecture | 474 | 148 |
| operations | 422 | 80 |
| failure-modes | 87 | 16 |
| scalability | 66 | 13 |
| documents-and-api | 47 | 9 |
| correctness | 23 | 21 |
| performance | 2 | 2 |
| **Total** | **3,407** | **920** |

Severity: 1,389 minor, 1,197 note, 821 major, 0 blocking.

| Repository | Entries before | Distinct before | Entries after | Distinct after | Holdout |
| --- | ---: | ---: | ---: | ---: | --- |
| pytest | 769 | 166 | 769 | 166 | yes |
| ripgrep | 645 | 157 | 592 | 146 | no |
| flask | 494 | 113 | 485 | 109 | no |
| fzf | 247 | 58 | 247 | 58 | no |
| httpx | 231 | 67 | 226 | 66 | no |
| requests | 201 | 60 | 191 | 54 | no |
| gin | 189 | 58 | 189 | 58 | yes |
| googletest | 164 | 60 | 131 | 44 | no |
| fd | 141 | 47 | 135 | 45 | no |
| got | 129 | 49 | 115 | 41 | no |
| fmt | 121 | 42 | 80 | 28 | no |
| express | 76 | 43 | 74 | 42 | no |

Holdouts account for 958 report entries (28.1%) and 224 distinct findings. They
were not used to choose any fix.

### Reports after regeneration

| Subject | Report entries | Distinct (repo, id) |
| --- | ---: | ---: |
| tests-and-coverage | 839 | 331 |
| dependencies | 690 | 124 |
| security | 593 | 121 |
| architecture | 474 | 148 |
| operations | 422 | 80 |
| failure-modes | 87 | 16 |
| scalability | 66 | 13 |
| documents-and-api | 47 | 9 |
| correctness | 14 | 13 |
| performance | 2 | 2 |
| **Total** | **3,234** | **857** |

Severity after regeneration: 1,373 minor, 1,146 note, 715 major, 0 blocking.
The reduction is concentrated in six verified false-positive/duplicate patterns
listed below; no subject was disabled and no severity was lowered.

A later follow-up on 2026-11-25 (see "Follow-up" below) removed 16
`stale_documented_api` entries and reclassified non-source deletions, so the
current corpus totals are 3,218 report entries and 854 distinct
`(repository, id)` findings. The tables in this first-pass section record the
state after the six original fixes, not the follow-up.

### Highest-volume types

| Subject / type | Before (entries / distinct) | After | Note |
| --- | ---: | ---: | --- |
| tests-and-coverage / `missing_test` | 565 / 301 | 459 / 256 | Range-only; graph-less filename fallback |
| operations / `workflow_action_not_commit_pinned` | 385 / 73 | 385 / 73 | Per-action line note; duplicated by `unpinned-action` |
| tests-and-coverage / `untested_module` | 375 / 74 | 375 / 74 | Whole-repo, filename/import fallback only |
| dependencies / `conflicting_dependency_versions` | 369 / 65 | 369 / 65 | Lockfile-resolved version diversity |
| architecture / `large_file` | 358 / 68 | 358 / 68 | Threshold fact, not a defect |
| dependencies / `duplicate_dependency_declaration` | 168 / 29 | 168 / 29 | Examples and workspace manifests |
| security / `unpinned-action` | 97 / 19 | 97 / 19 | Same condition as the operations rule |
| dependencies / `undeclared_import` | 87 / 17 | 87 / 17 | Includes dynamically created test modules |
| failure-modes / `silently_suppressed_exception` | 77 / 14 | 77 / 14 | Mostly test files |
| security / `dangerous_construct` | 77 / 15 | 77 / 15 | File-level summary of line rules |
| architecture / `breaking_change` | 69 / 69 | 18 / 18 | Deleted source/config/executable files; follow-up reclassified 51 docs/assets |
| architecture / `non_breaking_change` | 0 / 0 | 51 / 51 | Follow-up: deleted docs, images and other non-source files |
| scalability / `materialized_range` | 60 / 12 | 60 / 12 | Entirely pytest (holdout) |
| security / `exec` | 56 / 11 | 56 / 11 | Construct is real; message overclaims intent |
| security / `dependency-inventory` | 52 / 10 | 52 / 10 | Coverage statement, not a finding |
| security / `sensitive_file` | 50 / 10 | 50 / 10 | Path-only; public keys and test certs |
| security / `eval` | 46 / 9 | 46 / 9 | Same as `exec` |
| dependencies / `direct_source_dependency` | 42 / 7 | 0 / 0 | Fixed: internal workspace path deps |
| architecture / `todo_density` | 31 / 6 | 31 / 6 | Informational |
| documents-and-api / `stale_doc_reference` | 31 / 6 | 31 / 6 | One finding per doc; no line in JSON |

### Repetition is not duplication of defects

Of 920 distinct `(repository, id)` findings, 244 appeared in exactly one report
and 676 appeared in two or more. The most repeated appeared in six reports (a
repository has at most one whole-repo plus five range reports). A
whole-repo finding is intentionally copied into each range report so every
report stands alone. Consequently:

- raw "3,407 findings" is not 3,407 observations;
- ranking by raw entries over-weights whole-repo findings repeated up to six
  times;
- the corpus-level repetition is a report-layout property, not a rule defect.

## Sample evidence (non-holdout only)

| Category | Sampled location | What the pinned source shows | Classification |
| --- | --- | --- | --- |
| `missing_test` | express `lib/utils.js:290` | `test/utils.js` exists and requires `../lib/utils`. The fallback only knew Python/Go names. | Clear false positive (fixed by broader test-name fallback) |
| `missing_test` | flask `src/flask/app.py` | Exercised by `tests/test_basic.py`, but not by a file named after the module. | Known limitation of graph-less mode |
| `untested_module` | httpx `httpx/_client.py` | Tests live in `tests/client/test_client.py` and import the public `httpx` package, not `httpx._client`. | Known limitation; the finding note says a test may cover it under another name |
| `conflicting_dependency_versions` | fd `Cargo.lock` (`bitflags` 1.3.2 / 2.13.2) | Different dependents can resolve different major versions; this is normal Cargo resolution. | Duplicate/noise (inventory fact, not a conflict) |
| `duplicate_dependency_declaration` | flask `examples/celery/requirements.txt` and `pyproject.toml` | The example is a separate deployable environment. | Duplicate/noise |
| `workflow_action_not_commit_pinned` | fzf `.github/workflows/linux.yml:26` (`actions/setup-go@v7`) | The action is referenced by tag. Pinning is a policy choice. | Plausibly actionable, low priority |
| `unpinned-action` | same fzf workflow files | The security scanner reports the same files/lines as the operations rule. | Duplicate/noise (cross-subject) |
| `large_file` | flask `src/flask/app.py` (1,536 lines) | Factual threshold flag; refactoring may or may not be warranted. | Plausibly actionable, low priority |
| `undeclared_import` | flask `docs/conf.py` (`packaging`) | The docs config relies on a transitive Sphinx dependency; no manifest declares it. | Plausibly actionable (declare the docs dependency) |
| `undeclared_import` | flask `tests/test_instance_config.py` (`site_package`) | `site_package` is a directory created by the test and imported dynamically. | Clear false positive |
| `silently_suppressed_exception` | flask `tests/test_appctx.py:66` | `except Exception: pass` deliberately swallows a dummy exception in a test. | Duplicate/noise for test code |
| `silently_suppressed_exception` | flask `src/flask/config.py:163` | Broad handler around a JSON parse keeps the raw string; the comment states the intent. | Intentional/known limitation |
| `dangerous_construct` | flask `src/flask/config.py`, `src/flask/cli.py` | The same files/lines are reported by `exec`/`eval`. | Duplicate/noise |
| `breaking_change` | express `Collaborator-Guide.md`, flask `docs/_static/*.png` | Deleted docs/assets are typed as breaking changes (note severity). | Known limitation / mislabeled |
| `materialized_range` | pytest `testing/io/test_pprint.py` etc. | All 12 distinct findings are pytest, a holdout. | Excluded from tuning |
| `exec` / `eval` | flask `src/flask/config.py:209`, `src/flask/cli.py:1031` | `exec(compile(config))` loads a developer-selected config file; `eval` loads `PYTHONSTARTUP` in the interactive shell. | Intentional framework behavior; "attacker-controlled" overclaims |
| `dependency-inventory` | express `package.json` | States that components were inventoried and CVE status was not assessed. | Not actionable (coverage, not defect) |
| `sensitive_file` | requests `tests/certs/**/*.pem` and `*.key`, fmt `support/llvm-snapshot.gpg.key` | Test certificate/key fixtures (including a private key used only by tests) and an ASCII-armored PGP public key; path-only matching cannot tell test/public material from a live credential. | Duplicate/noise for test fixtures; clear false positive for the public key |
| `direct_source_dependency` | ripgrep `crates/*/Cargo.toml` (`path = "../matcher"`) | Sibling crates inside the same Cargo workspace. | Clear false positive (fixed) |
| `cookie-flags` | requests `src/requests/cookies.py:247` (`self.set_cookie(c)`) | A cookie-jar method, not an HTTP response cookie. | Clear false positive; only explicit `*jar` receivers are fixed |
| `permissive-mode` | googletest `googletest/src/gtest-filepath.cc:365` (`mkdir(..., 0777)`) | Deliberate cross-platform directory creation with the process umask. | Plausibly actionable, low priority |
| `stale_doc_reference` | flask `CHANGES.rst` | One finding for the whole changelog; the JSON has no line and the evidence is prose. | Known limitation; not verifiable from the finding alone |

These samples are evidence about rule behavior, not a precision estimate. The
sample is deliberately small and biased toward large categories.

## Duplicate-emission findings

1. **CI action pinning is reported twice** (`security/unpinned-action`,
   `operations/workflow_action_not_commit_pinned`). 333 of 385 operations
   entries are in files the security scanner also reports; 63 of 73 distinct
   operations findings and 13 of 19 distinct security findings share a file.
   This is 430 report entries for one condition. **Not changed**: the two
   collectors own different subject views and have different severities
   (`medium/high` vs `note/medium`). Choosing one owner is a product decision,
   not a clear rule bug. Recommended follow-up: one collector should own action
   pinning and the other should reference it.
2. **File-level `dangerous_construct` duplicates line-level rules.** 13 of 15
   distinct findings cover a file that also has `eval`, `exec`, `pickle`,
   `shell-true`, or a similar line finding. 11 of those 13 are in `pytest`
   (holdout); the non-holdout evidence is the two flask files in the sample
   table. The
   file-level summary is the fallback when the scanner is absent; it is
   redundant in a full deep review. Reported, not removed (it may cover
   constructs the line rules do not, and the pattern is holdout-heavy).
3. **`manifest-without-lockfile` (security) and `missing_lock_file`
   (dependencies)** describe the same missing lockfile. Small volume (10 vs 25
   entries); reported.
4. **Deleted binary file produced two findings**: `breaking_change` and
   `unknown` ("binary file changed") for the same deleted path. Fixed.
5. **Trust-boundary code flow duplicated `eval`/`exec`** at the same line.
   Fixed.
6. **`missing_test` duplicated `untested_module`** for some files in a range
   report (for example httpx `_client.py`). The whole-repo coverage check and
   the range-change check use different evidence. Reported; not merged.

## Fixes applied

All fixes are generic (no repository names), keep the original severity, and
have a regression test with a true-positive case.

| # | Defect | Change | Regression test |
| --- | --- | --- | --- |
| 1 | `weak-hash` flagged `hashlib.md5(..., usedforsecurity=False)` and its multi-line form | `security._extra_findings` skips an explicit non-security digest; an ordinary call still reports | `test_weak_hash_respects_explicit_non_security_flag` |
| 2 | `cookie-flags` flagged cookie-jar `set_cookie` calls as HTTP response cookies | `security._scan_source` ignores receivers whose name contains `jar`; a `response.set_cookie` without flags still reports | `test_cookie_flags_ignore_cookie_jar_receivers` |
| 3 | Trust-boundary `trust-flow-code` duplicated the `eval`/`exec` sink rule at the same line | added `code` to the existing collector dedupe map; an unbacked flow on another line remains | `test_trust_boundary_adapter_deduplicates_eval_sink_rule_only_at_same_line` |
| 4 | A deleted binary file emitted both `breaking_change` and `unknown` | skip the binary-content finding when the file is deleted; an added binary still reports `unknown` | `test_deleted_binary_file_has_one_deletion_finding` (plus the existing added-binary assertions) |
| 5 | Cargo/Go/Python path dependencies inside the checkout were called "direct URL or path" supply-chain bypasses | `_check_direct` skips relative paths that resolve inside the reviewed root; a path that leaves the root and a URL still report | `test_internal_workspace_path_dependency_is_not_a_direct_source` (plus the existing direct-source test) |
| 6 | `missing_test` only knew Python/Go test-name conventions, so JS/TS, Rust and C/C++ files with same-named tests were reported | `_fallback_tests` recognizes common JS/TS, Rust and C/C++ test names; a JS source with no matching test still reports | `test_missing_test_recognizes_javascript_and_cpp_test_names` |

Effect of the fixes: 173 fewer report entries (5.1%): 106 `missing_test`, 42
`direct_source_dependency`, 9 deleted-binary `unknown`, 6 `trust-flow-code`, 5
`weak-hash`, 5 `cookie-flags`. No new type, subject or severity was introduced.

Files changed:

- `src/ozagia/core/security.py`
- `src/ozagia/core/code_review.py`
- `src/ozagia/core/deep_review/collect.py`
- `src/ozagia/core/supply_chain.py`
- `tests/test_security.py`
- `tests/test_code_review.py`
- `tests/test_deep_review.py`
- `tests/test_supply_chain.py`

## Regeneration and repeatability

The 62 reports were regenerated with the documented command shape, using the
same pinned checkouts and no `--index`, model, network tool, target test or
target import:

```sh
/home/matt/ozma/ozagia/.venv/bin/ozagia deep-review \
  --root /home/matt/ozma/deep-review-corpus/repositories/<id> \
  --out /home/matt/ozma/deep-review-corpus/out/reports/<id>/<subject> \
  --profile full --budget-s 120 --fail-on never --json \
  [--range BASE..HEAD]
```

Packets were rebuilt from the existing pinned range metadata and repository
pins. `packet.json` report paths, SHA-256 values, counts, additional-range
findings, `instructions.md` references and `packet-index.json` were updated.
Reviewer A/B and adjudication files were left blank and unedited; no label file
was created or modified.

Verification:

- 62/62 commands exited 0; all 62 manifests remain `status: partial`.
- `packet-index.json`: 62 subjects, 12 whole-repo, 50 range, 62 manifests,
  3,234 findings, 0 failures.
- Every packet's recorded findings/manifest SHA-256 and count matches the file
  on disk.
- Repeat of `flask-01` into a separate directory produced byte-identical
  `findings.json` (SHA-256
  `f8c187a89c5f76f91ae8d62c4835eae3fae1cb3dbabc993e9964d4a8791c8c16`).
- `evaluation/deep_review_corpus/corpus.json` and its digest are unchanged;
  `ozagia.eval.deep_review_corpus validate` and `status` remain `valid` /
  `verified`.

## Follow-up: code examples and non-source deletions (2026-11-25)

Two further generic defects were fixed after the first regeneration. Neither
was chosen from `pytest` or `gin`; both holdouts keep the same entry and
distinct counts (pytest 769 / 166, gin 189 / 58).

| Metric | Before follow-up | After follow-up |
| --- | ---: | ---: |
| Report entries | 3,234 | 3,218 |
| Distinct `(repository, id)` | 857 | 854 |
| documents-and-api entries / distinct | 47 / 9 | 31 / 6 |
| `stale_documented_api` entries / distinct | 16 / 3 | 0 / 0 |
| `breaking_change` entries / distinct | 69 / 69 | 18 / 18 |
| `non_breaking_change` entries / distinct | 0 / 0 | 51 / 51 |
| Severity major / minor / note | 715 / 1,373 / 1,146 | 715 / 1,357 / 1,146 |

Only flask (485 -> 479 entries, 109 -> 108 distinct) and requests (191 -> 181
entries, 54 -> 52 distinct) change in entry/distinct counts. The 16 fewer entries
are all documentation examples; the deletion change reclassifies the same 69
deletion entries without removing any and without changing their severities.

### Documentation/API code examples

`docs_api` checked every line of Markdown as if an import statement were live,
so fenced examples (flask `README.md`) and indented examples (requests
`HISTORY.md`) produced `stale_documented_api` findings. The analyzer now walks
only prose lines: fenced blocks, indented code blocks and inline code spans are
masked or skipped, while a bare prose import line is still checked. Line numbers
are preserved for the prose findings that remain. Regression test:
`test_prose_import_is_checked_but_code_examples_are_not` keeps a missing prose
symbol reported and puts the same stale import in a fence, an inline span and an
indented block.

### Deletion classification

Deleted docs, images and other non-source files were typed `breaking_change`
(note severity), which is misleading. Deletions now classify by file kind and
git mode:

- source files: `breaking_change`, major (unchanged);
- config files: `breaking_change`, note (unchanged);
- deleted executables without a source suffix (git mode has an execute bit):
  `breaking_change`, note;
- generated files: `non_breaking_change`, note;
- tests: `missing_test` (unchanged);
- everything else (docs, images, data): `non_breaking_change`, note.

`non_breaking_change` is a new code-review type mapped to the architecture
subject in deep review. The diff-review task schema, Ocelot family mapping and
`docs/code_review.md` were updated with it. Regression tests:
`test_deleted_document_and_image_are_non_breaking`,
`test_deleted_source_config_and_executable_keep_breaking_classification` and the
updated `test_deleted_binary_file_has_one_deletion_finding`. After the change the
18 breaking deletions are 12 source majors and 6 config/executable notes; the 51
non-breaking deletions are all notes. No severity was lowered globally.

Files changed in this follow-up:

- `src/ozagia/core/deep_review/analyzers/docs_api.py`
- `src/ozagia/core/code_review.py`
- `src/ozagia/core/deep_review/range_review.py`
- `src/ozagia/core/ocelot_code.py`
- `src/ozagia/task_specs/coding/diff_review.toml`
- `tests/test_deep_review_docs_api.py`
- `tests/test_code_review.py`
- `docs/code_review.md`, `docs/ocelot_code.md`

### Follow-up regeneration

All 62 reports were regenerated with the same command shape, pins, budget and
`--fail-on never`; no `--index`, model, network tool, target test or target
import was used. Packets and `packet-index.json` were rebuilt from the existing
range metadata and repository pins. Verification:

- 62/62 commands exited 0; all 62 manifests remain `status: partial`.
- `packet-index.json`: 62 subjects, 12 whole-repo, 50 range, 62 manifests,
  3,218 findings, 0 failures.
- Every recorded findings/manifest SHA-256 and count matches the file on disk.
- Repeat of `flask-01` produced byte-identical `findings.json` (SHA-256
  `72e1435e0fe13b40ec7a457c20a7107de7ee10be7511c4b284c9ad12415f4669`).
- `evaluation/deep_review_corpus/corpus.json` and its digest are unchanged;
  validate/status remain `valid` / `verified`.
- A self-scan with the new analyzer produced no new `stale_documented_api` or
  `non_breaking_change` finding, so `ozagia/config.toml` was not changed.

This is still a noise/quality work note. It does not add a human label, a
precision/recall estimate or a measured go/no-go gate.

## Remaining noise and recommendations

Ranked by expected value of a change, not by raw count:

1. **`untested_module` / `missing_test` without a graph.** The findings are
   low-confidence filename/import heuristics; the titles still say "has no
   test". In the corpus, several flagged modules are tested under another name
   (flask `app.py`, httpx `_client.py`). Do not change the threshold or severity
   on this evidence. Prefer a graph-aware or test-execution run for a quality
   claim, and make the title match the evidence in a future release.
2. **`conflicting_dependency_versions` and
   `duplicate_dependency_declaration`.** These are inventory facts. Multiple
   resolved versions in a lockfile and a dependency declared in an example
   workspace are normal. If retained, rename them to "multiple resolved
   versions" / "declared in multiple manifests" or move them to inventory
   data rather than findings.
3. **Cross-subject CI-action duplication.** Pick one owner for action pinning;
   keep per-line operations detail or a security-level summary, not both.
4. **`large_file`, `todo_density`, `dependency-inventory`,
   `stale_doc_reference`.** Actionability is low. `dependency-inventory` is a
   coverage statement with a CWE tag; `stale_doc_reference` collapses all
   doc references to one line-less finding.
5. **Non-source deletions typed as `breaking_change`.** Resolved in the
   follow-up: docs, images and other non-source files are now
   `non_breaking_change` notes, while source, config and executable deletions
   remain breaking. Residual: these deletion findings are still generated (a
   caller that only wants source risk must ignore the type), and extensionless
   files rely on the git execute bit rather than content.
6. **Path-only credential/sensitive-file rules.** `.npmrc` files containing only
   `package-lock=false`, public PGP keys and test certificates are flagged as
   credentials. A content-aware suppression for configuration files and public
   key material is the next high-value fix, but it needs a careful
   private-key-versus-public-key decision.
7. **Cross-repository finding-ID collisions.** `finding_id` hashes subject,
   type, rule and location, not the repository. 14 IDs are shared by unrelated
   repositories (for example `package.json` rules). This does not affect a
   single report or its baseline, but an aggregate consumer must key by
   repository. Changing IDs would invalidate baselines, so this is reported
   only.

## Holdout exclusion

`pytest` and `gin` contribute 958 report entries (224 distinct) to the totals
above. They were not inspected to choose any fix. In particular,
`scalability/materialized_range` (60 entries, 12 distinct) comes entirely from
`pytest`; it is recorded as a holdout-only pattern and no change was made to it.
The fixes were justified from non-holdout repositories only: flask, express,
requests, httpx, ripgrep, fmt and fd supplied the sampled evidence. `got` and
`googletest` were inspected for other categories but did not drive a change. The
2026-11-25 follow-up also left both holdouts unchanged (958 entries, 224
distinct), and its documentary and deletion evidence came from flask, requests,
express and ripgrep.

## Limits

- **No human labels.** There is no ground truth, no precision, no recall and no
  false-positive rate. The four-way sample classification is rule-quality
  evidence, not scoring.
- **Partial reports.** Every report is `partial`: no graph snapshot, test
  execution, linter, type checker, model pass or network scanner. The corpus
  tests coverage and determinism, not repository quality.
- **Report entries are not unique findings.** Whole-repo findings repeat in
  range reports. Use `(repository, id)` distinct counts for observations and
  raw counts only for reviewer workload.
- **The sample is bounded and non-random.** It targets the largest categories
  and a few files per category; it cannot support a rate estimate.
- **No ground truth for "least actionable".** That judgment comes from reading
  the pinned source, not from labels.
- **The fixes are not a quality claim.** Removing 173 generic false-positive or
  duplicate entries does not make the remaining findings correct, and does not
  measure the go/no-go gates.
- **The corpus remains unlabeled.** The next step is still the two-reviewer plus
  adjudicator session in `evaluation/deep_review_corpus/README.md`; this audit
  does not replace it.
