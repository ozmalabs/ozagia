# Self-review of the 0.1.5 working tree (2026-11-24)

Status: deterministic, offline self-review of the uncommitted 0.1.5 tree. No
model pass, no network security tool, no Ocelot, no benchmark, no full test
suite and no bundle build ran in this pass. Nothing was committed, staged or
pushed. The tag/publish decision remains with a human.

## Scope and environment

- `HEAD` is `9a5162d` (`chore: start 0.1.5.dev0`), the only commit in
  `v0.1.4..HEAD`. All 0.1.5 work is uncommitted: 53 modified tracked files and
  17 untracked paths at the start of this review (more result files exist now).
- Developer `.venv`: CPython 3.13, editable `ozagia 0.1.5.dev0`, Linux x86_64.
- The graph was built/refreshed with `ozagia index --root .` (state directory
  only). The final review ran against a fresh snapshot:
  `18165` nodes, `59125` edges, head `9a5162d`, freshness `fresh`.
- Review commands used `nice -n 10`; all Python commands used
  `PYTHONPATH=src` and the declared `.venv`.

## Commands and exit status

Artifacts are under `/tmp/opencode/ozagia-self-review/`.

| Command | Exit | Result |
| --- | ---: | --- |
| `nice -n 10 env PYTHONPATH=src .venv/bin/python -m ozagia index --root .` | 0 | refreshed snapshot; 18165 nodes, 59125 edges |
| `nice -n 10 env PYTHONPATH=src .venv/bin/python -m ozagia review-repo --root . --json --out /tmp/opencode/ozagia-self-review/review_repo_final.json` | 0 | 151 findings; 0 blocking, 1 major, 117 minor, 33 note |
| `nice -n 10 env PYTHONPATH=src .venv/bin/python -m ozagia deep-review --root . --out /tmp/opencode/ozagia-self-review/deep --budget-s 180` | 0 | 364 findings; 0 blocking, 16 major, 259 minor, 89 note |
| `nice -n 10 env PYTHONPATH=src .venv/bin/python -m ozagia capabilities audit --json` | 0 | 0 findings; 102 capabilities, 156 surfaces |
| `nice -n 10 env PYTHONPATH=src .venv/bin/python -m ozagia.security_cli scan --root . --json --out /tmp/opencode/ozagia-self-review/security_scan.json` | 1 | 21 findings; 16 high, 3 medium, 2 low; all triaged below |
| `nice -n 10 env PYTHONPATH=src .venv/bin/python -m ozagia.supply_chain_cli check --root . --json` | 0 | 0 findings; 391 packages, 5 manifests, 1 lockfile |
| `nice -n 10 env PYTHONPATH=src .venv/bin/python -m ozagia.repo_scan_cli scan --root . --json --out /tmp/opencode/ozagia-self-review/repo_scan.json` | 0 | 25 of 853 candidates; 0 security findings |
| `nice -n 10 env PYTHONPATH=src .venv/bin/python -m ozagia iac check --root . --json` | 0 | 2 files, 1 fixture-only finding |
| `nice -n 10 env PYTHONPATH=src .venv/bin/python -m ozagia.code_review_cli --root . --range v0.1.4..HEAD --json --out /tmp/opencode/ozagia-self-review/review_delta_014_head.json` | 0 | 0 findings (only the `.dev0` chore commit) |
| `nice -n 10 env PYTHONPATH=src .venv/bin/python -m ozagia doctor --root . --json` | 0 | 3 integration warnings, no hard error |
| `nice -n 10 env PYTHONPATH=src .venv/bin/python -m ozagia.status_cli --root . --json` | 0 | `ok: true`, no errors, no degraded sections after reindex |
| `nice -n 10 env PYTHONPATH=src .venv/bin/python -m pytest -q tests/test_config_migrate.py tests/test_doctor_integrations.py tests/test_settings.py tests/test_status.py` | 0 | 100 passed in 1.45 s |
| `nice -n 10 .venv/bin/ruff check <changed/new Python files>` | 0 | all checks passed |

## Review-repo counts

`ozagia review-repo` reported 151 findings from 9 sections:

| Section | Findings |
| --- | ---: |
| architecture | 18 |
| dependencies | 7 |
| docs | 21 |
| health | 50 |
| security | 5 |
| tests | 50 |
| churn, ops | 0 |

The single major is the pre-existing package cycle:
`src/ozagia/core`, `daemon`, `edit`, `eval`, `harness`, `index`,
`integrations`, `intent`, `migration`, `model_api` and `research` import each
other (`package_cycles`, high confidence). Nothing in the 0.1.5 diff creates
or worsens it.

The 21 docs findings are stale links/code spans. Two of them are artifacts of
uncommitted work: `README.md` points at `docs/roadmap.md` and
`docs/status.md` points at `docs/integrations.md`; both files exist in the
working tree but are untracked, so the tracked-file resolver cannot see them.
They resolve once the files are added. The other 19 are pre-existing stale
references (for example `docs/eval_retrieval.md`, `docs/memory_mcp.md`,
`docs/rust.md`), not 0.1.5 regressions.

## Deep-review counts

`ozagia deep-review` reported 364 findings across 11 subjects (0 blocking):

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

The scan completed and exited 1 because 16 findings are at the default
`fail_on = high`. All 16 were read and none is a real secret or a 0.1.5
regression:

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
- **Delta review:** `v0.1.4..HEAD` contains only the `.dev0` chore commit and
  has 0 findings. It cannot review the uncommitted 0.1.5 work.

## Doctor and status

Both exit 0. `ozagia doctor` reports three expected warnings and no hard error:

- Codex `0.172.0` is outside the tested `0.169.0` (MCP table read only).
- Claude Code `2.1.227` has no tested live version (protocol fixtures only).
- The `~/.cargo/bin/rust-analyzer` shim exists but prints no version (state
  `unknown`, not `verified`).

`ozagia-status` reports `ok: true`, no errors and no degraded sections after
the final index. Its new `integrations` and `capabilities` sections are
bounded and contain no full findings, evidence or secret values.

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
`CHANGELOG.md` text and old plan notes (for example the `src/app.py` example in
`docs/plans/2026-11-16_ui_and_agent_features.md`).

## What remains

| Item | Owner / release |
| --- | --- |
| Commit all 0.1.5 changes and add the untracked paths before tagging. A tag at `HEAD` would omit the entire 0.1.5 feature set. | release manager, 0.1.5 |
| Add the untracked 0.1.5 docs so the two review-repo link findings resolve. | release manager, 0.1.5 |
| `ozagia-security scan` zero-high decision: resolved 2026-11-25 by rule fixes plus nine exact reviewed fingerprints; scan exits 0 with `high: 0`. | security, 0.1.5 |
| Pre-existing package cycle between `core`, `daemon`, `edit`, `eval`, `harness`, `index`, `integrations`, `intent`, `migration`, `model_api` and `research`. | architecture, post-0.1.5 |
| Pre-existing stale doc references (19 review-repo docs findings). | docs, 0.1.x backlog |
| Missing test links for 25 modules and 25 symbols; the new 0.1.5 modules do have focused tests. | tests, 0.1.x backlog |
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
  packaging and release checks; this pass ran only four targeted test modules
  (100 tests) and targeted ruff. The full-suite and build evidence is in
  `docs/results/release_check_0.1.5_2026-11-24.md`, not independently
  reproduced here.
- **Uncommitted diff:** `ozagia-review v0.1.4..HEAD` cannot see uncommitted or
  untracked work. Its 0 findings do not review 0.1.5.
- **Tracked-file blind spot:** `review-repo` only resolves tracked files, so
  untracked new docs were checked with an independent working-tree path pass.
  Likewise, test-link counts for new files may be understated until they are
  committed.
- **Synthetic versus realistic evidence:** the security findings are from
  fixtures, test strings and static rules. No real secret, no CVE feed, no
  exploitability analysis, no human labels and no live host session was
  involved.
- **Advisory outputs:** repo scan and deep review are ranked evidence, not
  proof of a defect. The capability audit is not an exhaustive coverage
  statement (`coverage.exhaustive` is always false).

## Review verdict

From the deterministic review perspective there is **no 0.1.5
release-blocking code defect**: `review-repo` has 0 blocking findings,
`deep-review` has 0 blocking findings, the capability audit and supply-chain
check are clean, the delta review is clean, doctor/status exit 0, and the
targeted tests and ruff pass. Every high security finding was checked and is a
false positive or an intentional installer message.

At review time the remaining blocker was procedural: the 0.1.5 tree was
uncommitted. The security gate has since been resolved as described above. The
complete tree is committed on 2026-11-25 before tagging, and the delta review
is re-run against that commit. From the deterministic review perspective 0.1.5
may then proceed to tagging, subject to the live-host gaps and the external
bundle smoke recorded in the release check.
