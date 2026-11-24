# Deep-review realistic corpus: acquisition, pins and reviewer packets (2026-11-24)

This note records corpus acquisition and deterministic report generation. It is
**not a quality result**. No human label exists, no prediction has been scored,
and the go/no-go gate status remains `not_measured`.

## Result at a glance

- 12 public repositories acquired and verified at release-tag commits.
- 12 licenses verified from the repository license file at the pinned commit.
- 50 of 50 change-range slots assigned to real ancestor `base..head` pairs.
- 62 deterministic reports generated (12 whole-repo + 50 range), 0 command
  failures, 3,407 findings in total across reports.
- 62 blank reviewer packets prepared (reviewer A, reviewer B, adjudicator);
  0 labels entered; 0 sealed label files.
- Manifest status: `acquired_pinned_ranges_assigned_no_labels`.
- Manifest digest:
  `5fae870cbb0d83b55c3904da97408a01f30af4cb116df072de9ec9120c1598c0`.

## Acquired pins

Canonical checkouts are full Git clones under
`/home/matt/ozma/deep-review-corpus/repositories/<id>` so that `--range` reviews
have history. GitHub archives were also downloaded with
`python -m ozagia.eval.deep_review_corpus acquire --allow-network` and their
SHA-256 recorded. Neither the corpus command nor review imported, tested or
executed target code.

| id | repository | release tag | pinned commit | verified license |
| --- | --- | --- | --- | --- |
| flask | `pallets/flask` | `3.1.3` | `22d924701a6ae2e4cd01e9a15bbaf3946094af65` | BSD-3-Clause |
| requests | `psf/requests` | `v2.34.2` | `6e83187b8feb273ed4c6cdab5efd8d54901dfab3` | Apache-2.0 |
| httpx | `encode/httpx` | `0.28.1` | `26d48e0634e6ee9cdc0533996db289ce4b430177` | BSD-3-Clause |
| pytest | `pytest-dev/pytest` | `9.2.0` | `ae1d0ff57b8bd4252da9ade7353523ac0ac73281` | MIT |
| ripgrep | `BurntSushi/ripgrep` | `16.0.0` | `669cb4c755db20b26c3af3d923c042ae224c7496` | MIT OR Unlicense |
| fd | `sharkdp/fd` | `v10.5.2` | `b2a287b3993768e0ec52f0852916917f23fb2a38` | MIT OR Apache-2.0 |
| fzf | `junegunn/fzf` | `v0.75.0` | `c7bf2b8eea25952dd257a466c0afa72faae206fb` | MIT |
| gin | `gin-gonic/gin` | `v1.12.0` | `73726dc606796a025971fe451f0aa6f1b9b847f6` | MIT |
| express | `expressjs/express` | `v5.3.0` | `cf6722bf89a86905281d9160b6482e08166b5ef0` | MIT |
| got | `sindresorhus/got` | `v16.2.0` | `6b5aafc1cd538998728be6f45c65b0f80bae01b2` | MIT |
| fmt | `fmtlib/fmt` | `12.2.1` | `208219daff58f2a7438f6931cc2ddc358acf33ef` | MIT |
| googletest | `google/googletest` | `v1.18.0` | `063de7e9578f82b369302001269680b4b1553359` | BSD-3-Clause |

Full `pinned_commit`, `tree_sha256`, `archive_sha256`, `verified_at`, measured
languages and size class are in `evaluation/deep_review_corpus/corpus.json`.
No candidate was dropped.

### Archive-tree caveat

The safe archive extractor intentionally skips symlinks and GitHub applies
`export-subst`. For three repositories the archive extraction tree therefore
does not equal the Git checkout tree:

- `requests`: two `tests/certs/.../ca` symlinks are omitted.
- `pytest`: `.git_archival.txt` is substituted by `export-subst`.
- `ripgrep`: the `HomebrewFormula` symlink is omitted.

The Git checkout is canonical for review and for `tree_sha256`. The
`archive_sha256` still records the exact downloaded archive. All 12
repositories verify as `verified` against their commit and tree pins.

## Range assignment

All 50 slots are `assigned`; none is `labeled`. Ranges use real release-tag
commits where the pair is a direct ancestor. Where consecutive release tags sit
on divergent release branches (`pytest`, `googletest`), the base is the
merge-base of the two tags and the head is the later tag, or an earlier ancestor
tag pair is used. Every range now satisfies the product's `git.base_ancestor`
check.

Each slot records its full `base`/`head` commit IDs, theme, focus and a factual
rationale in `corpus.json`. Slot counts per repository: flask 5, ripgrep 5, and
4 for each of the other ten repositories.

An initial pass selected four non-ancestor release-tag pairs. The range review
correctly refused them (`git.base_ancestor`) and recorded the refusal as
coverage; those four reports were re-assigned to ancestor pairs and regenerated.
The corrected reports have `range_review.status: available`.

## Deterministic reports

Command shape for every subject:

```sh
/home/matt/ozma/ozagia/.venv/bin/ozagia deep-review \
  --root /home/matt/ozma/deep-review-corpus/repositories/<id> \
  --out /home/matt/ozma/deep-review-corpus/out/reports/<id>/<subject> \
  --profile full --budget-s 120 --fail-on never --json \
  [--range BASE..HEAD]
```

No `--index`, model pass, network tool, target test, target import or build was
used. All 62 commands exited 0; durations were 0.52–6.33 s per report. Total
findings across report files: 3,407 (514 in whole-repo reports, 2,893 in range
reports). A repeat of `flask-01` into a separate report directory produced a
byte-identical `findings.json` (SHA-256
`9870d8e88ce501ed6a29576875e5c3971ed5cebf1d754f9c445ad1c1a88ee5b8`). Every
report manifest status is `partial` because no graph snapshot, test execution,
linter or type checker was available; this is coverage, not a quality claim.

| repository | whole-repo findings | range findings | additional vs whole-repo |
| --- | ---: | ---: | ---: |
| flask | 73 | 421 | 56 |
| requests | 33 | 168 | 36 |
| httpx | 37 | 194 | 46 |
| pytest | 150 | 619 | 19 |
| ripgrep | 80 | 565 | 165 |
| fd | 16 | 125 | 61 |
| fzf | 47 | 200 | 12 |
| gin | 31 | 158 | 34 |
| express | 6 | 70 | 46 |
| got | 15 | 114 | 54 |
| fmt | 12 | 109 | 61 |
| googletest | 14 | 150 | 94 |

"Additional vs whole-repo" counts finding IDs in a range report that are absent
from the repository's whole-repo report. It is an observation about these
report files, not a verified range-only defect count.

## Reviewer packets

External packet root: `/home/matt/ozma/deep-review-corpus/out/packets`.
Index: `/home/matt/ozma/deep-review-corpus/out/packet-index.json`.

Each of the 62 packet directories contains:

- `packet.json`: repository/subject metadata, pin and license, range diff
  summary and digest, changed-path list, report/manifest paths and SHA-256,
  finding counts and range-review status.
- `instructions.md`: static-review rules, independent-pass rule, sealing
  command and adjudication steps.
- `reviewer-a.json`, `reviewer-b.json`, `adjudication.json`: blank files derived
  from the versioned templates. Reviewer IDs are explicit placeholders and all
  label/decision arrays are empty.

The packet index records acquisition details, report counts, per-subject counts
and paths. It does not copy source text. No file under
`evaluation/deep_review_corpus/labels/` was added or filled.

## Product defect fixed

`acquire_repository` wrote `.ozagia-deep-review-corpus.json` into the extracted
tree and then verified the tree including that marker. The digest had been
computed before the marker existed, so every freshly acquired archive reported
`tree_status: mismatch` despite its marker saying `tree_verified: true`.
The marker is now excluded from `ozagia-tree-v1`
(`TREE_DIGEST_EXCLUDES`), and a regression test asserts that adding the marker
does not change the digest. This is a small, clearly scoped corpus-acquisition
fix; no review logic changed.

## Checks run

```sh
PYTHONPATH=src .venv/bin/python -m ozagia.eval.deep_review_corpus validate
PYTHONPATH=src .venv/bin/python -m ozagia.eval.deep_review_corpus status \
  --checkout-root /home/matt/ozma/deep-review-corpus
PYTHONPATH=src .venv/bin/python -m ozagia.eval.deep_review_corpus verify \
  --repo <id> --checkout /home/matt/ozma/deep-review-corpus/repositories/<id>

PYTHONPATH=src .venv/bin/python -m ozagia.eval.deep_review_corpus_labels validate \
  --labels evaluation/deep_review_corpus/labels

.venv/bin/python -m pytest -q \
  tests/test_eval_deep_review_corpus.py \
  tests/test_eval_deep_review_corpus_labels.py \
  tests/test_eval_deep_review_corpus_score.py

.venv/bin/ruff check src/ozagia/eval/deep_review_corpus.py \
  tests/test_eval_deep_review_corpus.py
```

Results:

- Manifest validation: valid, 12 verified repositories, 50 assigned ranges,
  0 labeled ranges, 2 holdout repositories.
- Checkout status: 12 verified, 0 missing, 0 unverified, 0 mismatch.
- Per-repository `verify`: all 12 `verified` with commit and tree checks.
- Label validation: `valid`, 0 reviewer files, 0 adjudication files, 0
  subjects; no label was entered.
- Corpus tests: 43 passed.
- Ruff: no issues.

## Limitations and failures

- No human label exists. Precision, recall, false-positive rate, noise,
  actionability and every go/no-go gate are unmeasured.
- Every report is `partial`: graph-dependent evidence, tests, linters and type
  checks were not used. Reports therefore say what was checked and omitted;
  they do not establish code quality.
- The three archive-tree differences above are documented rather than hidden.
- `pytest` and `gin` remain holdouts. Their reports and packets exist, but they
  must not be used to tune rules, thresholds or label conventions.
- Range report counts include the whole-repo review plus range findings. The
  "additional vs whole-repo" column is a report comparison, not ground truth.
- Corpus status is not a release gate. `score_gate_status` stays
  `not_measured`.

## Later regeneration (2026-11-25)

The 62 reports and packets were regenerated on 2026-11-25 after an internal
noise audit fixed six generic false-positive or duplicate-emission patterns.
The pins, tree/archive digests and manifest digest did not change. The same
command shape was used and every report remains `partial`. Totals after
regeneration: 3,234 findings across the 62 reports (was 3,407), with the
`flask-01` repeat still byte-identical. The counts and per-range tables above
describe the original 2026-11-24 run; see
[`deep_review_noise_audit_2026-11-25.md`](deep_review_noise_audit_2026-11-25.md)
for the audited categories, sample evidence, fixes and the current packet-index
counts.

## Exact human next steps

1. Open `out/packet-index.json` and choose a non-holdout subject. Have reviewer
   A and reviewer B review the pinned checkout independently and fill their own
   packet files. Neither reviewer may open `out/reports/...` before both files
   are sealed.
2. Seal both reviewer files, then have the adjudicator fill and seal
   `adjudication.json`. Copy only completed, sealed files into
   `evaluation/deep_review_corpus/labels/<repository-id>/<subject-key>/`.
3. Validate the label set with
   `python -m ozagia.eval.deep_review_corpus_labels validate --labels evaluation/deep_review_corpus/labels`
   (add `--require-human --require-complete` only for the final frozen set).
4. Build a run record from the packet index and report files, including report
   and manifest SHA-256 values, completeness, graph freshness, durations,
   exit status and writes-outside-allowed-paths. Mark runs `partial` where the
   report says partial.
5. Run the scorer with `--require-realistic` only when every required subject
   has complete human labels. Publish raw numerators/denominators, exclusions
   and limitations. Do not treat a passing aggregate as universal correctness.
6. Re-run acquisition verification before scoring if any checkout, report or
   manifest is regenerated, and update the manifest digest.
