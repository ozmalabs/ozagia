# 0.1.5 release verification (2026-11-24, re-run 2026-11-25)

Status: verification of the 0.1.5 stabilization tree, re-run after the
security-hardening changes. The checked source was committed as `4d3ff1f`
after these runs; no source changed between the checks and that commit except
result-note edits. Nothing was tagged, pushed or published, no live model or
host-agent session ran, and no network request was made. The tag/publish
decision remains with a human.

## Environment

- The checked tree was `9a5162d` plus the 0.1.5 stabilization and
  security-hardening edits; it is now commit `4d3ff1f`.
- Developer `.venv`: CPython 3.13.12, editable `ozagia 0.1.5.dev0`, Linux
  x86_64, `uv 0.10.12`; `git`, `jq 1.8.2` and `ripgrep 15.2.0` on `PATH`.
- PyYAML came from the declared `.venv`. `uv build --offline` used the local
  cache only.

## Gates

| Gate | Command | Result |
| --- | --- | --- |
| Full suite | `PYTHONPATH=src .venv/bin/python -m pytest -q` | pass: `6436 passed, 29 skipped` in 441.09 s (exit 0) |
| Four shards | `OZAGIA_TEST_SHARD=i/4 PYTHONPATH=src .venv/bin/python -m pytest -q` | pass: shards 1–4 in 118 s, 104 s, 107 s and 104 s; `1449+1775+1828+1368 = 6420` passed, `45` skipped |
| Shard collection | `OZAGIA_TEST_SHARD=i/4 ... --collect-only -q` | `1463 + 1779 + 1836 + 1383 = 6461`, exactly the full collection |
| Security scan | `PYTHONPATH=src .venv/bin/python -m ozagia.security_cli scan --root . --json` | pass (exit 0): `high: 0`, `allowed: 9`, five advisory findings (3 medium, 2 low), no errors or warnings |
| Ruff | `.venv/bin/ruff check src tests scripts` | pass, no findings |
| Whitespace | `git diff --check` | pass |
| Shell syntax | `bash -n install scripts/release/smoke.sh` | pass |
| Workflow YAML | PyYAML `safe_load` of `ci.yml`, `release.yml` | pass; jobs: ci `test`; release `test`, `dist`, `bundle`, `release` |
| Release metadata | `pyproject.toml`, `git tag --list v0.1.5`, `CHANGELOG.md` | pass: version `0.1.5.dev0`, no `v0.1.5` tag, `## Unreleased` present |
| Build | `uv build --wheel --sdist --offline -o /tmp/opencode/ozagia-dist-security .` | pass: wheel and sdist built offline |
| Doctor | `PYTHONPATH=src .venv/bin/python -m ozagia.cli doctor --root . --json` | pass (exit 0); capabilities current, three expected warnings |
| Status | `PYTHONPATH=src .venv/bin/python -m ozagia.status_cli --root . --json` | pass (exit 0); `ok: true`, no errors, degraded only on `snapshot.stale` |
| Capability audit | `PYTHONPATH=src .venv/bin/python -m ozagia.cli capabilities audit --json` | pass (exit 0): 102 capabilities, 156 surfaces, 0 findings |
| Config migrate | `ozagia config migrate --root <tmp> --dry-run --json` | pass: 10 moves, 8 project-model tables, token issued, no write; apply with the token in the temp copy wrote the tables and left the legacy file untouched |

## Test-count reconciliation

`--collect-only` lists 6461 test items. Pytest also counts four module-level
`pytest.importorskip("substrate_rs")` skips
(`test_research_finite_formula.py`, `test_research_formula.py`,
`test_research_formula_resolution.py`, `test_research_native.py`), which it does
not print as collected items. The JUnit run therefore has 6465 test cases: 6461
regular items (`6436 passed`, `25 skipped`) plus the four module skips. The
shard collections sum to 6461 and the four shard runs also total 6465 cases,
but their composition differs: 6420 passed and 45 skipped.

The 16 extra passes in the unsharded run are optional-backend tests in
`tests/test_research_service_port.py` and `tests/test_research_trace.py`. They
call `pytest.importorskip("substrate_rs")`, which succeeds only after
`tests/test_research_ocelot_reconcile.py` runs an in-process Ocelot pass. That
path temporarily puts the sibling `ocelot/runners/dsl/python` on `sys.path`
and leaves the imported `substrate_rs` in `sys.modules`. In a shard without
that earlier module, and on a host without a sibling Ocelot checkout (as in
CI), the same tests skip in both modes. Every run is green; this is a
host-specific optional-backend ordering leak, not a product failure or a
missing test.

## Security hardening (2026-11-25)

- `eval`/`exec` matches inside Python strings, f-string literal segments and
  comments are masked; real calls, including `f"{eval(value)}"` and files that
  cannot be tokenized, stay findings.
- `generic_high_entropy` ignores key-loader calls and dotted references while
  keeping quoted and bare literals.
- `unsafe-yaml` proves `Loader=yaml.SafeLoader`/`BaseLoader` and same-file safe
  subclasses; dynamic imports, `FullLoader`/`UnsafeLoader` and bare
  `yaml.load(value)` stay findings.
- `[security] finding_allowlist` applies exact `sha256:<16 hex>` fingerprints
  over rule, normalized path and normalized matched line. It is separate from
  the secret gate's `secret_allowlist`, is matched by equality (not prefix),
  and allowed findings are excluded from severity counts and `--fail-on`.
- The repository's untracked `ozagia/config.toml` carries nine reviewed exact
  fingerprints; the scan matched all nine (`allowed: 9`) and reported no config
  errors or warnings.
- Five non-allowlisted advisory findings remain (3 medium, 2 low), below
  `fail_on = "high"`: archive extraction in `build_bundle.py`,
  non-cryptographic randomness in `evaluation/m6_live/schedule.json`, MD5/SHA-1
  in `core/fingerprint.py`, the dependency inventory (an inventory, not a CVE
  claim), and secret-in-log in `console.py`.

The scan JSON and text report contain rule names, paths, line numbers and
fingerprints only. They were checked for common token/key shapes (GitHub,
OpenAI, AWS, bearer, password assignment) with zero hits; the allowlisted
canary values are not reproduced.

## Artifacts

Built with `uv build --offline` from the working tree:

| Artifact | Path | Size | Contents |
| --- | --- | --- | --- |
| Wheel | `/tmp/opencode/ozagia-dist-security/ozagia-0.1.5.dev0-py3-none-any.whl` | 2,773,020 bytes (2.6 MiB) | 392 files; `ozagia/` package and `ozagia-0.1.5.dev0.dist-info/`; no `docs/`, no `config.toml` |
| sdist | `/tmp/opencode/ozagia-dist-security/ozagia-0.1.5.dev0.tar.gz` | 6,647,478 bytes (6.3 MiB) | 1152 files; includes `docs/security.md`, this note, `CHANGELOG.md` and the repo `ozagia/config.toml` |

The wheel contains the new `ozagia/core/security.py`, `ozagia/config.py`,
`ozagia/integrations/versions.py`, `ozagia/integrations/presence.py`, the
deep-review corpus modules and `ozagia/core/migrate.py`; it has no `__pycache__`
entries. Metadata reports `Version: 0.1.5.dev0`. `pyproject.toml` packages only
`src/ozagia` in the wheel, so docs and the repository's `ozagia/config.toml`
are not package data. The config appears in the sdist only as part of the
source tree, which is the intended handling. `ozagia doctor` prints
`docs/integrations.md` as a matrix reference, which is a documentation path,
not a runtime file read.

## Prepare and changelog

- `scripts/release/prepare.py 0.1.5 --dry-run` on this tree refuses exactly:
  `working tree has uncommitted changes` (exit 1). This is the designed guard,
  not a script failure.
- The same script was run in a small simulated clean repository (copies of
  `pyproject.toml`, `CHANGELOG.md` and `prepare.py`, a `main` branch and a
  `v0.1.4` tag). It printed
  `version 0.1.5.dev0 -> 0.1.5, tag v0.1.5, branch main` and
  `changelog: the '## Unreleased' section becomes '## 0.1.5 (2026-11-24)'`,
  exited 0, and made no write.
- The version sort key was checked directly: `0.1.5` is newer than
  `0.1.5.dev0`, which is newer than `0.1.4`.
- `scripts/release/notes.py 0.1.5` currently prints `See CHANGELOG.md.` because
  the section is still `## Unreleased`; after `prepare.py` renames it, the
  section regex finds the drafted prose (checked on the renamed text).
- The `## Unreleased` section now carries a draft `0.1.5` entry. `prepare.py`
  supplies the date and heading. The draft explicitly labels live host sessions
  and the bundle/install smoke as not tested.

## Runtime observations

`doctor` reported three warnings, all expected on this host:

- `integration.codex.version`: Codex hooks 0.172.0 is outside the tested range
  (`0.169.0`, MCP table read only).
- `integration.claude.version`: Claude Code hooks 2.1.227 has no tested live
  version (protocol fixtures only).
- `integration.rust_analyzer.unknown`: the `~/.cargo/bin/rust-analyzer` shim
  exits 1 without a version; `kiro-cli` is not installed.

The doctor snapshot is stale because this checkout has no fresh graph index;
`ozagia-status` reports that as the only degraded item. A pre-existing eval
daemon (pid 683649, socket under `/tmp/ozagia-eval-*.sock`) was visible to
status; this check did not start it.

`capabilities audit` reports 102 capabilities in 14 domains and 156 declared
surfaces with zero findings; eight capabilities have no declared surface, which
is an inventory fact, not a finding.

`ozagia-status` reports `config: configured` because `ozagia/config.toml` now
exists for the reviewed security allowlist. The file is currently untracked, so
it must be added to the 0.1.5 commit; it is repository configuration, not
package data.

`config migrate` on a temporary copy of
`tests/fixtures/config_migration/legacy.toml` previewed 10 moves and these eight
tables: `environments.dev`, `environments.staging`, `services.api`,
`services.worker`, `relays.prod`, `runtime.services.api`,
`runtime.services.migrate`, `runtime.tunnels.database`. The dry run created no
`ozagia/config.toml` and issued a 64-character token; applying with that token
wrote all eight tables and left `.ozagia.toml` unmodified.

## Not verified or network-gated

- **Live host sessions:** Codex 0.172.0, Claude Code 2.1.227, Kiro (not
  installed) and OpenCode Desktop. No model tokens were spent.
- **Bundle and installer smoke:** blocked by design without network.
  `scripts/release/build_bundle.py` downloads the pinned CPython
  3.13.18+20261110 archive from python-build-standalone (no
  `~/.cache/ozagia-release` on this host) and pip-installs the platform extras
  from PyPI. `scripts/release/smoke.sh` additionally downloads OpenCode 2.3.0
  from GitHub even when the bundle itself is served over `file://`. Both were
  inspected and syntax-checked but not run.
- **Realistic deep-review quality:** the corpus is scaffolding; no public
  repository is acquired, no human label is entered and no realistic score or
  go/no-go exists. That remains 0.2.0 work.

No release-blocking defect was found. The tree is ready for an 0.1.5 commit
once the untracked files (including `ozagia/config.toml`, `docs/roadmap.md` and
this note) are added; `prepare.py` still requires a clean tree. The remaining
work is external (live hosts and a network-enabled bundle smoke) or belongs to
0.2.0.
