# Self-review of 0.1.5 (2026-11-24, committed re-review 2026-11-25)

Status: deterministic, offline self-review of 0.1.5. The first pass reviewed
the then-uncommitted tree; this report was updated after 0.1.5 was committed as
`4d3ff1f` on 2026-11-25. No model pass, no network security tool, no Ocelot, no
benchmark, no full test suite and no bundle build ran in this review. Nothing
was committed, staged or pushed by this review. The tag/publish decision
remains with a human.

## Scope and environment

- The committed range is `v0.1.4..4d3ff1f` (`4d3ff1f` is
  `chore(release): prepare 0.1.5`; base `8cf4cbc` is `Release v0.1.4`).
  The commit changes 104 files: 45 added, 59 modified, 11,667 insertions and
  178 deletions.
- Developer `.venv`: CPython 3.13, editable `ozagia 0.1.5.dev0`, Linux x86_64.
- The graph was built/refreshed with `ozagia index --root .` (state directory
  only). The committed re-review ran against a fresh snapshot:
  `18212` nodes, `59346` edges, head `4d3ff1f`, freshness `fresh`.
- Review commands used `nice -n 10`; all Python commands used
  `PYTHONPATH=src` and the declared `.venv`.

## Commands and exit status

Artifacts are under `/tmp/opencode/ozagia-self-review/`.

| Command | Exit | Result |
| --- | ---: | --- |
| `nice -n 10 env PYTHONPATH=src .venv/bin/python -m ozagia index --root .` | 0 | refreshed snapshot; 18212 nodes, 59346 edges |
| `nice -n 10 env PYTHONPATH=src .venv/bin/python -m ozagia review-repo --root . --json --out /tmp/opencode/ozagia-self-review/review_repo_committed.json` | 0 | 155 findings; 0 blocking, 1 major, 120 minor, 34 note |
| `nice -n 10 env PYTHONPATH=src .venv/bin/python -m ozagia deep-review --root . --out /tmp/opencode/ozagia-self-review/deep --budget-s 180` | 0 | 364 findings; 0 blocking, 16 major, 259 minor, 89 note (first pass, before commit) |
| `nice -n 10 env PYTHONPATH=src .venv/bin/python -m ozagia capabilities audit --json` | 0 | 0 findings; 102 capabilities, 156 surfaces |
| `nice -n 10 env PYTHONPATH=src .venv/bin/python -m ozagia.security_cli scan --root . --json --out /tmp/opencode/ozagia-self-review/security_scan_committed.json` | 0 | 5 findings; 0 high, 3 medium, 2 low; 9 allowed |
| `nice -n 10 env PYTHONPATH=src .venv/bin/python -m ozagia.supply_chain_cli check --root . --json` | 0 | 0 findings; 391 packages, 5 manifests, 1 lockfile |
| `nice -n 10 env PYTHONPATH=src .venv/bin/python -m ozagia.repo_scan_cli scan --root . --json --out /tmp/opencode/ozagia-self-review/repo_scan.json` | 0 | 25 of 853 candidates; 0 security findings |
| `nice -n 10 env PYTHONPATH=src .venv/bin/python -m ozagia iac check --root . --json` | 0 | 2 files, 1 fixture-only finding |
| `nice -n 10 env PYTHONPATH=src .venv/bin/python -m ozagia.code_review_cli --root . --range v0.1.4..HEAD --json --out /tmp/opencode/ozagia-self-review/review_delta_014_4d3ff1f.json` | 0 | 9 findings; 0 blocking, 6 major, 3 minor |
| `nice -n 10 env PYTHONPATH=src .venv/bin/python -m ozagia doctor --root . --json` | 0 | 3 integration warnings, no hard error |
| `nice -n 10 env PYTHONPATH=src .venv/bin/python -m ozagia.status_cli --root . --json` | 0 | `ok: true`, no errors; one `snapshot.stale` because this report edit dirties the tree after the index |
| `nice -n 10 env PYTHONPATH=src .venv/bin/python -m pytest -q tests/test_capabilities.py tests/test_config_migrate.py tests/test_security.py tests/test_settings.py tests/test_status.py tests/test_doctor_integrations.py tests/test_eval_deep_review_corpus.py` | 0 | 180 passed in 2.92 s |
| `nice -n 10 .venv/bin/ruff check <changed/new Python files>` | 0 | all checks passed |

The deep-review, repo-scan and IaC rows are from the first pass; the source
code is unchanged by the commit, and they were not rerun in the requested
scope. Review-repo, security, doctor/status and the delta review were rerun
against `4d3ff1f`.

## Review-repo counts

`ozagia review-repo` reported 155 findings on the committed tree:

| Section | Findings |
| --- | ---: |
| architecture | 18 |
| dependencies | 7 |
| docs | 24 |
| health | 50 |
| security | 5 |
| tests | 50 |
| churn | 1 |
| ops | 0 |

The single major is still the pre-existing package cycle:
`src/ozagia/core`, `daemon`, `edit`, `eval`, `harness`, `index`,
`integrations`, `intent`, `migration`, `model_api` and `research` import each
other (`package_cycles`, high confidence). Nothing in the 0.1.5 diff creates
or worsens it.

The two uncommitted-link findings are gone: `README.md` -> `docs/roadmap.md`
and `docs/status.md` -> `docs/integrations.md` now resolve because both files
are committed. The docs count rose from 21 to 24: minus those two, plus five
new false positives caused by the committed root `ozagia/` configuration
directory. `docs/code_review.md:85`, `docs/models.md:299`,
`docs/resources.md:93`, `docs/settings.md:964,968-969` and
`docs/worktree.md:69` name target-project configuration files (a project's
worktrees, models and policies files under its own `ozagia/` directory); the
resolver now treats that first component as this repository's `ozagia/` and
reports them missing. They are examples, not broken links. The other 19 are
the same pre-existing stale references (for example
`docs/eval_retrieval.md`, `docs/memory_mcp.md`, `docs/rust.md`), not 0.1.5
regressions.

### Committed delta re-review (2026-11-25)

Command and artifact:

```sh
PYTHONPATH=src .venv/bin/python -m ozagia.code_review_cli --root . \
  --range v0.1.4..HEAD --json \
  --out /tmp/opencode/ozagia-self-review/review_delta_014_4d3ff1f.json
```

Exit status 0. The range is base `8cf4cbc` (`v0.1.4`) to head `4d3ff1f`
(`chore(release): prepare 0.1.5`), covering 104 files
(+11,667/-178). It reports **9 findings: 0 blocking, 6 major, 3 minor**, with
3 heuristic library hints:

| ID | Severity | Type | Location | Triage |
| --- | --- | --- | --- | --- |
| CR-001 | major | `missing_test` | `src/ozagia/core/capabilities.py:1535` | False positive: `tests/test_capabilities.py` imports `from ozagia.core import capabilities`; the graph links the package `__init__`, not the submodule. |
| CR-002 | major | `missing_test` | `src/ozagia/core/migrate.py:4` | False positive: `tests/test_config_migrate.py` imports `from ozagia.core import migrate as M`. |
| CR-003 | major | `missing_test` | `src/ozagia/core/security.py:104` | False positive: `tests/test_security.py` imports `from ozagia.core import security as S`; its changed scanner tests pass. |
| CR-004 | major | `missing_test` | `src/ozagia/core/settings.py:133` | False positive: `tests/test_settings.py` imports `from ozagia.core import settings as S`. |
| CR-005 | major | `missing_test` | `src/ozagia/core/status.py:66` | False positive: `tests/test_status.py` imports `from ozagia.core import status as status_module`. |
| CR-006 | major | `missing_test` | `src/ozagia/integrations/versions.py:1` | False positive: `tests/test_doctor_integrations.py` imports `from ozagia.integrations import versions`. |
| CR-007 | minor | `security_sensitive` | `src/ozagia/core/security.py:104` | Path-token flag on the security module itself; expected, not a defect. |
| CR-008 | minor | `security_sensitive` | `src/ozagia/core/security.py:459` | The matched text is docstring prose about masking `f"eval(x)"`, not an executable `eval`. |
| CR-009 | minor | whitespace | `tests/test_eval_deep_review_corpus.py:237` | **Real**: one new blank line at EOF. `git diff --check v0.1.4..HEAD` exits 2. |

The six `missing_test` findings share one graph limitation: a test that uses
`from package import module` is linked to `package/__init__.py`, not to the
submodule file. Every changed module has a covering test that passes:
`tests/test_capabilities.py`, `tests/test_config_migrate.py`,
`tests/test_security.py`, `tests/test_settings.py`, `tests/test_status.py` and
`tests/test_doctor_integrations.py` passed together (164 tests), and the
catalogue/status declarations are also exercised by the capability audit.
The corpus test file passes on its own (16 tests); only its final blank line
is a whitespace defect.

So the committed delta has **0 blocking findings and no release-blocking code
defect**. It is not literally zero-finding: six majors are false positives
from test-link resolution, two minors are false-positive security tokens, and
one minor is a real one-line whitespace fix.

## Deep-review counts

`ozagia deep-review` reported 364 findings across 11 subjects (0 blocking) in
the first pass. The source code is unchanged in the commit; these findings
were not rerun for the committed-delta update.

| Subject | Findings | Major | Status |
| --- | ---: | ---: | --- |
| security | 44 | 15 | partial |
| architecture | 68 | 1 | partial |
| failure-modes | 59 | 0 | partial |
| tests-and-coverage | 50 | 0 | partial |
| dependencies | 43 | 0 | partial |
| documents-and-api | 42 | 0 | partial |
| scalability | 31 | 0 | partial |
| operations | 17 | 0 | partial |
| performance | 6 | 0 | partial |
| data-structures | 4 | 0 | partial |
| correctness | 0 | 0 | unknown (no collector) |

Every deep-review major was checked against the code:

| Major | Location | Evidence |
| --- | --- | --- |
| `eval` | `src/ozagia/cli.py:218` | The match is prose inside the `DELEGATES` help string ("agent eval (python ..."). There is no `eval()` call. Scanner false positive. |
| `curl-pipe-shell` (3) | `src/ozagia/selfinstall.py:234`, `scripts/release/notes.py:36,38` | The first is the documented installer/upgrade path; the other two are printed release-note instructions, not executed. Pre-existing and intentional. |
| `pickle` | `src/ozagia/core/deep_review/collect.py:330` | Bounded result received over a private multiprocessing pipe from ozagia's own isolated analyzer child. The bytes are not hostile input; accepted internal protocol. |
| `marshal` | `src/ozagia/core/search.py:200` | Reads ozagia's own state-directory search cache; corruption is caught and the cache rebuilt. Accepted. |
| `unsafe-yaml` (4) | `core/pulumi_graph.py:128`, `edit/yaml_edit.py:85`, `core/openapi.py:72`, `core/deployment_graph.py:102` | Three use an explicit `SafeLoader` subclass; `openapi.py` uses `BaseLoader`; `yaml_edit.py` uses ruamel `YAML(typ="rt")`. No unsafe loader is used. Scanner cannot see custom loaders. |
| `github_token`, `openai_key`, `url_credentials` (3) | `addins/opencode/tests/runner.test.cjs:554,555,558` | Synthetic strings in a redaction test (`ghp_...`, `sk-...`, `https://user:pass@...`). Not real secrets and not 0.1.5 changes. |
| `generic_high_entropy` (3) | `evaluation/m6_live/run_m6.py:872`, `harness/native.py:568`, `harness/review.py:952` | The lines are assignments from key-reader calls (`read_api_key(...)`, `_read_api_key(...)`, `_load_api_key(...)`), not embedded secrets. False positive. |
| `package_cycle` | `src/ozagia/core/__init__.py` | The pre-existing package cycle described above. |

The deep documents-and-api findings are also false positives: they treat code
examples such as `from ozagia.core import evidence` as if they were live API
imports. The operations notes are pre-existing action-pinning, workflow-permission,
runbook and healthcheck advisories, not 0.1.5 changes.

## Capability audit

`ozagia capabilities audit --json`:

- 102 capabilities in 14 domains, 156 declared surfaces.
- 0 findings; 0 undeclared surfaces; 0 duplicate declarations.
- 8 capabilities have no declared surface (an inventory fact, not a finding).
- Audit state `current`; catalogue hash
  `350434131cb6c5506b5e127c7b0f9d8d1261a157e3dbb96b6c31da34dab02103`.

`ozagia doctor` and `ozagia-status` report the same catalogue counts and 0
audit findings.

## Security scan

The initial pass (uncommitted tree) exited 1 because 16 findings met the
default `fail_on = high`. All 16 were read and none is a real secret or a
0.1.5 regression. The committed re-scan after the reviewed rule fixes and
exact allowlist exits 0 with `high: 0`, `allowed: 9` and five medium/low
advisories; the follow-up below records the details. The table is retained as
the original triage:

| Group | Count | Location | Triage |
| --- | ---: | --- | --- |
| Provider keys / URL credentials | 3 | `addins/opencode/tests/runner.test.cjs:554,555,558` | Synthetic redaction-test values |
| Generic high entropy | 3 | `evaluation/m6_live/run_m6.py:872`, `harness/native.py:568`, `harness/review.py:952` | Variable assignment from a key loader, not a literal |
| Code execution (`eval`) | 1 | `src/ozagia/cli.py:218` | Prose in a help string |
| Unsafe YAML | 4 | `core/pulumi_graph.py:128`, `edit/yaml_edit.py:85`, `core/openapi.py:72`, `core/deployment_graph.py:102` | Custom safe loaders |
| Deserialization | 2 | `core/deep_review/collect.py:330`, `core/search.py:200` | Private pipe and local cache |
| Unverified download | 3 | `selfinstall.py:234`, `scripts/release/notes.py:36,38` | Installer and printed instructions |

The medium/low findings are pre-existing/advisory: archive extraction in
`scripts/release/build_bundle.py:89`, a non-security randomness match in
`evaluation/m6_live/schedule.json`, SHA-1 use for a fingerprint in
`core/fingerprint.py:144`, dependency inventory without CVE data, and a
secret-logging heuristic in `console.py:289`.

No real credential was found. Secret contents of binary files, git history and
external stores were not scanned.

### Follow-up: security gate resolved (2026-11-25)

The 16 highs above were handled without broad suppressions:

- the `eval`/`exec` rule now ignores matches wholly inside Python strings and
  comments, while f-string expressions and real calls still flag;
- `generic_high_entropy` no longer flags an assignment from a key-loader call;
- `unsafe-yaml` proves a safe `Loader=` argument or a same-file safe subclass
  instead of skipping on any nearby `SafeLoader`;
- nine intentional findings (three redaction-test canaries, the installer and
  two printed release-note instructions, the private-pipe pickle, the local
  cache marshal and the ruamel round-trip YAML load) are exact
  `sha256:` fingerprints in `ozagia/config.toml`'s
  `[security] finding_allowlist`. Editing the matched line invalidates the
  entry.

`PYTHONPATH=src .venv/bin/python -m ozagia.security_cli scan --root . --json`
now exits 0 with `high: 0`, `allowed: 9` and five medium/low advisories. The
targeted security/config tests pass; the full-suite and packaging evidence is
in the release check. The original triage above is retained as the review
record.

## Supply chain, repo scan, IaC and delta

- **Supply chain:** 0 findings. 391 packages were inventoried with integrity
  data, 5 manifests and 1 lockfile; 0 packages lacked integrity. CVE state is
  unknown because no advisory data was supplied.
- **Repo scan:** 25 advisory opportunities of 853 candidates (10 hotspots, 8
  missing test links, 7 weak boundaries). 0 security findings. 828 candidates
  were omitted by design. The scan reported `status: degraded` because the
  working tree was dirty at scan time.
- **IaC:** the repository has no deployable Terraform/OpenTofu of its own. The
  only `.tf` files are `tests/fixtures/iac_repo/infra/*.tf`; `iac check` found
  one `missing_provider_requirement` in the fixture. Fixture-only, not a
  release blocker.
- **Delta review:** `v0.1.4..4d3ff1f` is the actual 0.1.5 delta and reports
  9 findings (0 blocking, 6 major, 3 minor), all triaged in the committed
  delta subsection above. It no longer reviews only the `.dev0` chore commit.

## Doctor and status

Both exit 0. `ozagia doctor` reports three expected warnings and no hard error:

- Codex `0.172.0` is outside the tested `0.169.0` (MCP table read only).
- Claude Code `2.1.227` has no tested live version (protocol fixtures only).
- The `~/.cargo/bin/rust-analyzer` shim exists but prints no version (state
  `unknown`, not `verified`).

`ozagia-status` reports `ok: true` and no errors. It reports one
`snapshot.stale` because this report edit makes the working tree dirty after
the index; that is a freshness signal, not a code failure. Its new
`integrations` and `capabilities` sections are bounded and contain no full
findings, evidence or secret values.

## What was fixed in this pass

Two stale status claims contradicted the implemented 0.1.5 work:

1. `docs/roadmap.md` (plan audit row for
   `docs/plans/2026-11-16_ui_and_agent_features.md`) still listed project-model
   config migration as remaining. It now records that
   `ozagia config migrate` landed in 0.1.5 and keeps only the live-host,
   live-model and DAST gaps.
2. The `docs/plans/2026-11-16_ui_and_agent_features.md` status header still said
   `ozagia config migrate` could not emit `[environments]`, `[services]`,
   `[relays]` or `[runtime]`. It now says the migration landed, and points at
   `src/ozagia/core/migrate.py` and `docs/settings.md`.

The targeted tests and ruff checks above passed after the edits. A working-tree
path/anchor check over the changed docs and the new
`docs/integrations.md`, `docs/roadmap.md`,
`docs/results/deep_review_corpus_scaffold_2026-11-24.md`,
`docs/results/release_check_0.1.5_2026-11-24.md` and the corpus README found no
new broken links or stale path spans. The remaining path-span hits are
pre-existing illustrative examples in `docs/testing.md`, historical
`CHANGELOG.md` text and old plan notes (for example the illustrative app/test
paths in `docs/plans/2026-11-16_ui_and_agent_features.md`).

## What remains

| Item | Owner / release |
| --- | --- |
| Fix the trailing blank line at `tests/test_eval_deep_review_corpus.py:237`; `git diff --check v0.1.4..HEAD` currently exits 2. One-line follow-up commit or an explicit decision to accept it. | release manager, 0.1.5 |
| `ozagia-security scan` zero-high decision: resolved 2026-11-25 by rule fixes plus nine exact reviewed fingerprints; scan exits 0 with `high: 0`. | security, 0.1.5 |
| Pre-existing package cycle between `core`, `daemon`, `edit`, `eval`, `harness`, `index`, `integrations`, `intent`, `migration`, `model_api` and `research`. | architecture, post-0.1.5 |
| Review-repo docs findings: 24 total; 19 pre-existing stale references and 5 false positives from target-project example paths shadowed by the new root `ozagia/` directory. | docs, 0.1.x backlog |
| Test-link gaps: 25 modules and 25 symbols in the whole-repo review; the six delta `missing_test` findings are false positives from `from package import module` link resolution. | tests, 0.1.x backlog |
| GitHub Actions pinned by mutable tags and CI workflow permissions not declared (deep-review notes). | operations, later release |
| Live Codex, Kiro, Claude Code and OpenCode Desktop sessions; network-gated bundle/installer smoke; realistic deep-review quality. | 0.1.5 external gate / 0.2.0 |

## Limitations

- **Graph snapshot:** final review ran on a fresh snapshot. Writing this result
  file makes the working tree dirty again, so `ozagia-status` may report
  `snapshot.stale` until the next index. That is a freshness signal, not a
  code failure.
- **No model pass:** `--model-pass`, `--live`, `--model`, `--security-tool`,
  `--security-network`, `--run-bench` and Ocelot options were not used. The
  deep review is deterministic only.
- **No full suite or bundle build:** another agent ran the full suite,
  packaging and release checks; this pass ran 180 targeted tests over the
  changed areas and targeted ruff. The full-suite and build evidence is in
  `docs/results/release_check_0.1.5_2026-11-24.md`, not independently
  reproduced here.
- **Committed delta scope:** the delta review now covers `v0.1.4..4d3ff1f`.
  It does not include working-tree changes made after the commit (this report
  edit, or any later whitespace fix). A separate uncommitted change to
  `src/ozagia/eval/deep_review_corpus.py` is also present in the working tree;
  it is outside this committed-delta review and was not made by this review.
- **Graph test-link blind spot:** the graph does not resolve a
  `from package import module` test import to the submodule file, so six
  changed modules appear as `missing_test` even though their covering tests
  exist and pass. This is a review heuristic limitation, not proof of
  untested code.
- **Synthetic versus realistic evidence:** the security findings are from
  fixtures, test strings and static rules. No real secret, no CVE feed, no
  exploitability analysis, no human labels and no live host session was
  involved.
- **Advisory outputs:** repo scan and deep review are ranked evidence, not
  proof of a defect. The capability audit is not an exhaustive coverage
  statement (`coverage.exhaustive` is always false).

## Review verdict

From the committed-delta review perspective there is **no 0.1.5
release-blocking code defect**. The committed range `v0.1.4..4d3ff1f` has
0 blocking findings; `review-repo` has 0 blocking findings, the capability
audit and supply-chain check are clean, doctor/status exit 0, `ozagia-security
scan` now exits 0 with `high: 0` and 9 allowed reviewed fingerprints, and the
targeted tests and ruff pass.

It is not literally zero-finding. The delta review reports 9 findings:

- 6 major `missing_test` false positives caused by `from package import
  module` test-link resolution; all six changed modules have covering tests
  that pass (164 targeted tests);
- 2 minor `security_sensitive` false positives (the security module path and a
  docstring that discusses `eval` masking);
- 1 minor real whitespace defect: a trailing blank line at
  `tests/test_eval_deep_review_corpus.py:237`, which makes
  `git diff --check v0.1.4..HEAD` exit 2.

The only remaining review asks are that one-line whitespace fix (or an
explicit decision to accept it) and the external/live-host gaps recorded in
the release check. From the committed-delta review perspective, 0.1.5 is clean
for release: 0 blocking findings and no code defect. Tagging may proceed once
the release manager records the whitespace decision and the external gates.
