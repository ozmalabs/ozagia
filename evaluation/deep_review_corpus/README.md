# Realistic deep-review corpus (acquired, pinned, unlabeled)

This directory holds the frozen realistic deep-review corpus for the evaluation
proposed in
[`docs/results/deep_review_go_no_go_proposal_2026-11-18.md`](../../docs/results/deep_review_go_no_go_proposal_2026-11-18.md).
All 12 public repositories are acquired and verified at real release-tag
commits, and all 50 change-range slots are assigned to real `base..head` pairs.
**No human label has been entered and no quality gate is measured.** Acquisition
and report generation are not a quality result.

The checkouts, reports and blank reviewer packets live outside this repository
(under a caller-selected checkout root such as
`/home/matt/ozma/deep-review-corpus`). Only the manifest, templates, label
placeholder and documentation are versioned here.

The existing synthetic fixtures (`tests/fixtures/deep_review_*`) remain separate
regression checks. Their scores must never be pooled into a realistic-corpus
score. The scorer enforces one evidence class per invocation and marks synthetic
runs as not applicable to quality gates.

## Files

| Path | Purpose |
| --- | --- |
| `corpus.json` | Versioned manifest: 12 verified public repositories pinned to release-tag commits, 50 assigned real change ranges, inclusion reasons, measured language/size facts. No labels and no scores. |
| `templates/reviewer-a.template.json` | Blank independent reviewer file. |
| `templates/reviewer-b.template.json` | Second independent reviewer file. |
| `templates/adjudicator.template.json` | Blank adjudication file with decisions. |
| `labels/<repository-id>/<subject-key>/` | Human label files go here. `subject-key` is `whole-repo` or a range slot id. Each directory needs two reviewer files and one `adjudication.json`. The directory currently contains only a README: no human labels exist yet. |
| `src/ozagia/eval/deep_review_corpus.py` | Manifest validation, tree digest, offline checkout verification, explicit-network archive acquisition. |
| `src/ozagia/eval/deep_review_corpus_labels.py` | Label schema validation, sealing and label-set consistency checks. |
| `src/ozagia/eval/deep_review_corpus_score.py` | Deterministic scoring, coverage reporting and go/no-go gate computation. |

## Manifest schema (schema_version 1)

Top-level fields:

- `corpus_id`, `description`, `created`, `status`, `evidence_class`
  (`realistic_corpus` or `synthetic_regression`).
- `targets`: at least 10 repositories and 50 change ranges for a realistic
  corpus; `holdout_repositories` and optional `complete_repositories_for_quality`.
- `review_policy`: exactly 2 reviewers, one adjudicator, independent review
  before seeing Ozagia findings, sealed label files, source-file digests, and
  separation from synthetic fixtures.
- `repositories[]`:
  - `id`, GitHub `repo` slug, `remote` (must be
    `https://github.com/<owner>/<name>`), `license`, `license_status`.
  - `acquisition_status`: `candidate_unacquired`, `acquired` or `verified`.
  - `pinned_commit`, `tree_sha256`, `archive_sha256`, `verified_at`.
    A `candidate_unacquired` entry must have all four null. An acquired entry
    must have a commit and tree digest; a verified entry also needs
    `verified_at`.
  - `languages`, `language_mix_status`, `size_class`, `size_status`, `reason`,
    `local_checkout` (relative to the checkout root), `holdout`.
- `change_ranges[]`:
  - `id`, `repository_id`, `status` (`slot_unassigned`, `assigned`, `labeled`,
    `excluded`), `base`/`head`, `theme`, `review_focus`, `source_hint`,
    `reason`, `holdout`.
  - Unassigned slots must have null endpoints. Assigned/labeled slots need two
    distinct 40- or 64-character commits. Every repository must have at least
    one slot.

Two repositories are marked holdout (`pytest-dev/pytest` and `gin-gonic/gin`).
Do not tune rules, thresholds or label conventions on holdout repositories; use
the non-holdout repositories for development and report holdout results
separately. A passing aggregate must not hide a weak holdout subject.

## Acquisition state (2026-11-24)

Every repository is `verified` against a release-tag commit. The manifest
records the full commit, `tree_sha256` (the deterministic `ozagia-tree-v1` digest
of the canonical Git checkout), `archive_sha256` (the downloaded GitHub
archive), `verified_at`, measured languages and size class. License files were
read at the pinned commit; SPDX names below are the verified expression.

| id | repository | pin | license | canonical checkout | GitHub archive |
| --- | --- | --- | --- | --- | --- |
| flask | `pallets/flask` | `3.1.3` (`22d92470`) | BSD-3-Clause | Git checkout | tree matches |
| requests | `psf/requests` | `v2.34.2` (`6e83187b`) | Apache-2.0 | Git checkout | extraction omits two test-cert symlinks |
| httpx | `encode/httpx` | `0.28.1` (`26d48e06`) | BSD-3-Clause | Git checkout | tree matches |
| pytest | `pytest-dev/pytest` | `9.2.0` (`ae1d0ff5`) | MIT | Git checkout | `export-subst` changes `.git_archival.txt` |
| ripgrep | `BurntSushi/ripgrep` | `16.0.0` (`669cb4c7`) | MIT OR Unlicense | Git checkout | extraction omits the `HomebrewFormula` symlink |
| fd | `sharkdp/fd` | `v10.5.2` (`b2a287b3`) | MIT OR Apache-2.0 | Git checkout | tree matches |
| fzf | `junegunn/fzf` | `v0.75.0` (`c7bf2b8e`) | MIT | Git checkout | tree matches |
| gin | `gin-gonic/gin` | `v1.12.0` (`73726dc6`) | MIT | Git checkout | tree matches |
| express | `expressjs/express` | `v5.3.0` (`cf6722bf`) | MIT | Git checkout | tree matches |
| got | `sindresorhus/got` | `v16.2.0` (`6b5aafc1`) | MIT | Git checkout | tree matches |
| fmt | `fmtlib/fmt` | `12.2.1` (`208219da`) | MIT | Git checkout | tree matches |
| googletest | `google/googletest` | `v1.18.0` (`063de7e9`) | BSD-3-Clause | Git checkout | tree matches |

The three archive-tree differences are properties of the extraction path, not
of the commit: GitHub archives carry `export-subst` substitutions, and the
existing safe extractor intentionally skips symlinks. The canonical review
checkout is therefore the Git checkout, which retains history for `--range`
reviews. `archive_sha256` still records the exact downloaded archive so the
acquisition is auditable.

The 50 range slots are `assigned` to real release-tag diffs. Each range records
both full commit hashes, its theme, focus and a factual rationale in
`corpus.json`. Ranges are ancestor pairs; where consecutive release tags live on
divergent release branches, the base is the merge-base of the two tags and the
head is the later tag. No label is attached to any slot.

```sh
# Verify every pinned checkout offline.
python -m ozagia.eval.deep_review_corpus status \
  --checkout-root /home/matt/ozma/deep-review-corpus

# Verify one repository.
python -m ozagia.eval.deep_review_corpus verify \
  --repo flask --checkout /home/matt/ozma/deep-review-corpus/repositories/flask
```

Reports and reviewer packets are external. The packet index records report and
manifest paths/digests, finding counts, the exact diff digest and the blank
reviewer files, but not source text. See
[`docs/results/deep_review_corpus_acquisition_2026-11-24.md`](../../docs/results/deep_review_corpus_acquisition_2026-11-24.md).

Do not change a commit pin or tree digest without re-running `verify` and
recording the new `verified_at`. Do not invent a commit or a label to make the
manifest look complete.

## Acquisition and verification

Everything is offline by default:

```sh
# Validate the manifest and print summary counts/digests.
python -m ozagia.eval.deep_review_corpus validate

# Verify local checkouts that already exist. This never downloads.
python -m ozagia.eval.deep_review_corpus status \
  --checkout-root /path/to/checkouts

# Verify one checkout against the commit/tree pins in the manifest.
python -m ozagia.eval.deep_review_corpus verify \
  --repo flask --checkout /path/to/flask

# Download one pinned GitHub archive. Requires an explicit network flag.
python -m ozagia.eval.deep_review_corpus acquire \
  --repo flask --allow-network --checkout-root /path/to/checkouts
```

`acquire` refuses an unassigned commit before making any network request. It
uses the existing bounded GitHub archive path (`hf_benchmarks._download` and
`extract_archive`), rejects path traversal and unsafe archive members, writes
outside the target checkout only its temporary staging directory, and never
imports or executes target code. With `--allow-network` absent, a missing
checkout is a `network_not_allowed` refusal rather than a fallback.

The tree digest algorithm is `ozagia-tree-v1`: SHA-256 over sorted records of
`kind\0relative-path\0content-or-link-target`, where kind is `f` or `l`, regular
file content is hashed, symlinks are hashed by target and never followed, and
top-level `.git`/`.ozagia` plus the acquisition marker
`.ozagia-deep-review-corpus.json` are excluded. A Git checkout also records
`git rev-parse HEAD`; an archive checkout has no Git metadata, so its commit pin
is the manifest's declaration and its tree digest is the independent check.

To promote a candidate after human acquisition:

1. Select a clean public checkout at a known commit.
2. Run `verify` and record `head_commit` and `tree.sha256`.
3. For an archive acquisition, record the marker's `archive_sha256`.
4. Set `acquisition_status` to `verified`, fill `pinned_commit`,
   `tree_sha256`, `archive_sha256` (when applicable) and `verified_at`.
5. Re-run `validate`.

## Label schema (label_schema_version 1)

Copy the two reviewer templates and the adjudicator template into
`labels/<repository-id>/<subject-key>/` and rename them `reviewer-a.json`,
`reviewer-b.json`, `adjudication.json` (the role in the file, not the filename,
is authoritative).

Shared envelope:

- `label_schema_version`, `corpus_id`, `repository_id`, `subject_key`.
- `kind`: `reviewer` or `adjudicator`.
- `provenance`: `human` or `synthetic_test_fixture`. Realistic scoring requires
  `human`.
- `reviewer`: `{id, role, independent_pass, started_at, completed_at}`.
  Timestamps are ISO-8601 with a timezone and are required for a completed file.
- `declared_scope`: `{status: complete|partial|unknown, description, subjects,
  paths}`. A complete scope must state its boundary and at least one subject.
- `label_file_sha256`: canonical digest of the file with this field removed.
  Seal after every edit with:
  ```sh
  python -m ozagia.eval.deep_review_corpus_labels seal \
    --path labels/<repo>/<subject>/reviewer-a.json \
    --out labels/<repo>/<subject>/reviewer-a.json
  ```
- Reviewer files have `labels[]`; adjudication files have `decisions[]`.

Each label/decision records:

- `id` (unique in its file), `subject`, `type`, `severity`
  (`blocking|major|minor|note|unknown`).
- `location`: relative `path`, optional `start_line`/`end_line` and `symbol`.
  Exact scopes require a start line; symbol scopes require a symbol.
- `scope`: `exact|file|symbol|repository|unknown`.
- `verdict`: `true_positive|false_positive|undetermined`.
- `evidence[]`: `{kind, reference, digest, note}` where kind is
  `diff|file|line|symbol|test|issue|command_output|manual`. A true positive
  needs evidence.
- `claim`, `actionable: {is_actionable, wording}`, `severity_appropriate`,
  `source_file_digest` (SHA-256 of the reviewed file at the pinned commit), and
  optional `notes`.

Adjudication decisions additionally carry `reviewer_label_ids` (reviewer id →
referenced label ids), `resolution`
(`agree|reviewer_a|reviewer_b|new|excluded`) and `rationale`. Every reviewer
label must be referenced by at least one decision; a referenced label id must
exist. A partial scope can be scored for recall only; predictions from a partial
scope stay unlabeled and contribute to noise, never to precision.

## Human labeling session runbook

1. Freeze the review environment: record the Ozagia source revision, Python
   version, collector configuration, corpus manifest digest, and the checkout
   commit/tree digest.
2. Acquire each repository through the workflow above. Do not run its tests,
   import it, or let it execute code. For each range, create a worktree or
   archive copy at `base..head` and record both commits.
3. Copy the two reviewer templates. Reviewer A and Reviewer B label the diff and
   directly affected code **before either sees an Ozagia report**. Use the same
   agreed scope; record `declared_scope` as `complete` only when that scope was
   fully examined.
4. Each reviewer records subject, type, severity, location/scope, evidence,
   actionable wording, severity-appropriateness, source-file digest, reviewer
   id and timestamps. Seal the file.
5. After both independent passes are sealed, compare them and have the
   adjudicator fill `adjudication.json`. Resolve every disagreement or mark it
   `undetermined`/`excluded`; link every reviewer label to a decision. Seal it.
6. Validate the sets:
   ```sh
   python -m ozagia.eval.deep_review_corpus_labels validate \
     --labels evaluation/deep_review_corpus/labels \
     --require-human --require-complete
   ```
   Until the corpus is complete this command is expected to report incomplete
   scopes. Do not label a scope complete just to silence the warning.
7. Run the deterministic reviews with the existing command, using an explicit
   `--out`, fixed `--budget-s`, no model pass and no benchmark execution:
   ```sh
   ozagia deep-review --root CHECKOUT --out REPORT_DIR \
     --range BASE..HEAD --budget-s 60 --json
   ```
   Never fetch or execute target code as part of the review. Preserve the raw
   `findings.json` and `manifest.json`.
8. Write a run record (`record.json`) with:
   - `schema_version: 1`, `corpus_id`, `evidence_class`, `scored_condition`
     (`whole_repo|range|graph_aware|graph_less`), optional `source_revision`,
     `environment`, `manifest_digest`, `labels_digest`.
   - `runs[]` with `run_id`, `repository_id`, `subject_key`, `condition`,
     `status` (`complete|partial|failed`), `report_path` relative to the record,
     `report_sha256`, optional `manifest_path`/`manifest_sha256`,
     `subject_status`, `graph_freshness`, `budget_s`, `duration_s`,
     `exit_status`, `writes_outside_allowed_paths`, and `repeat_of` for repeats.
   - Optional `human_utility`: `{actionability_rate, sample_size, method}` from
     the separate blinded top-finding sample.
9. Score:
   ```sh
   python -m ozagia.eval.deep_review_corpus_score \
     --manifest evaluation/deep_review_corpus/corpus.json \
     --labels evaluation/deep_review_corpus/labels \
     --record /path/to/record.json \
     --checkout-root /path/to/checkouts \
     --require-realistic \
     --out /path/to/score.json
   ```
   `--require-realistic` rejects synthetic evidence. The result keeps labeled
   and unlabeled predictions separate, reports raw numerators/denominators,
   per-repository and per-subject rows, digest checks, repeatability and the
   proposed gates. A gate status of `insufficient_data` is not a pass.

## Go/no-go computation

The scorer implements the proposed gates as deterministic checks:

- `deterministic_report_quality`: precision ≥ 0.80, recall ≥ 0.70, at least 8
  of 10 repositories complete.
- `high_impact_findings`: precision ≥ 0.90 and no unsupported blocking finding;
  every blocking reviewed label must have evidence and both reviewers.
- `subject_stability`: no subject with at least 20 predictions below 0.65
  precision.
- `noise`: complete-scope false positives / complete-scope predictions ≤ 0.20;
  the overall ratio including unlabeled scope is reported separately.
- `repeatability_and_coverage`: repeated frozen inputs stable, all pinned
  digests verified, no stale graph and no missing run for a complete label
  scope.
- `operational_bounds`: at least 95% of runs within budget and no writes
  outside allowed paths.
- `human_utility`: at least 80% actionability on the separate blinded sample;
  supplied via the run record, otherwise `not_measured`.

A repository counts as complete only when its `whole-repo` scope and every
non-excluded manifest range slot for that repository have complete labels and a
scored run. A partial range keeps the repository incomplete and is reported as
coverage, not silently averaged away.

The output never converts a passing gate into a claim of universal correctness.
Even a fully measured pass is only `gate_met_not_universal_correctness`; the
default `--fail-on never` does not change. Synthetic evidence is always
`not_applicable_synthetic` with quality claim `none`.

## Current status and limitations

- `corpus.json` is an acquired and verified manifest: 12 real release-tag pins,
  12 tree digests, 12 archive digests, 50 assigned ranges, 0 labels and 0 scored
  gates. `status` is `acquired_pinned_ranges_assigned_no_labels`.
- Deterministic whole-repo and per-range reports exist in the external output
  root (62 reports: 12 whole-repo and 50 range). Every report is `partial`
  because no graph snapshot, test execution, linter or type checker was used;
  this is coverage, not a quality claim.
- The run record and labels under `tests/fixtures/deep_review_corpus_dry_run/`
  are synthetic test data with fictional reviewer identities and
  `provenance: synthetic_test_fixture`. They exercise the pipeline only and are
  never pooled into this corpus.
- Source-file digests require a checkout. Without `--checkout-root`, digest
  status is `not_checked`, and the repeatability/coverage gate is incomplete.
- False-positive rate remains undefined: the labels do not enumerate every
  actual negative. Recall is relative to reviewed labels, not all defects.
- Three repositories (`requests`, `pytest`, `ripgrep`) have an archive
  extraction tree that differs from the Git checkout tree because the safe
  extractor skips symlinks and GitHub applies `export-subst`. The Git checkout
  and its tree digest are canonical; the archive digest is provenance only.
- Two holdout repositories (`pytest`, `gin`) must not be used to tune rules,
  thresholds or label conventions.
- Do not publish private repository contents or reviewer identities without
  authorization. The protocol publishes aggregate counts and digests, not
  private source excerpts.
