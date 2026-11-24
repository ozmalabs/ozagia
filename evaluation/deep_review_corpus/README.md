# Realistic deep-review corpus (candidate scaffolding)

This directory holds the versioned candidate corpus for the realistic
deep-review evaluation proposed in
[`docs/results/deep_review_go_no_go_proposal_2026-11-18.md`](../../docs/results/deep_review_go_no_go_proposal_2026-11-18.md).
It is **scaffolding, not a measured result**. The repository list, licenses and
sizes are candidate declarations; no pinned commit has been recorded yet and no
human label has been entered.

The existing synthetic fixtures (`tests/fixtures/deep_review_*`) remain separate
regression checks. Their scores must never be pooled into a realistic-corpus
score. The scorer enforces one evidence class per invocation and marks synthetic
runs as not applicable to quality gates.

## Files

| Path | Purpose |
| --- | --- |
| `corpus.json` | Versioned manifest: 12 candidate public repositories, 50 change-range slots, inclusion reasons, language/size declarations. All candidates are `candidate_unacquired` with null commits and digests. |
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

Two candidates are marked holdout (`pytest-dev/pytest` and `gin-gonic/gin`).
Do not tune rules, thresholds or label conventions on holdout repositories; use
the non-holdout candidates for development and report holdout results
separately. A passing aggregate must not hide a weak holdout subject.

`corpus.json` intentionally records no commit and no digest. A human selects a
public base/head pair, records it, and only then can acquisition run. Do not
invent a commit to make the manifest look complete.

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
top-level `.git`/`.ozagia` are excluded. A Git checkout also records
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

- `corpus.json` is an unacquired candidate manifest. It has no pinned commits,
  no tree digests and no human labels. Its candidate licenses are declared but
  not verified offline.
- The run record and labels under `tests/fixtures/deep_review_corpus_dry_run/`
  are synthetic test data with fictional reviewer identities and
  `provenance: synthetic_test_fixture`. They exercise the pipeline only.
- Source-file digests require a checkout. Without `--checkout-root`, digest
  status is `not_checked`, and the repeatability/coverage gate is incomplete.
- False-positive rate remains undefined: the labels do not enumerate every
  actual negative. Recall is relative to reviewed labels, not all defects.
- Do not publish private repository contents or reviewer identities without
  authorization. The protocol publishes aggregate counts and digests, not
  private source excerpts.
