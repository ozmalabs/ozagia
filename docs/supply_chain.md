# Supply-chain attack detection

Status: first-class offline v1, 2026-11-23. The analyzer is deterministic,
read-only and network-free. It reads dependency inputs and reports review
candidates; it never fetches, installs, imports or executes project code, and
the absence of a finding is not a clean verdict.

## Commands

```bash
ozagia supply-chain check [--root R] [--json] [--policy PATH] \
    [--fail-on critical|high|medium|low|note|none] \
    [--budget-s N] [--max-findings N]

ozagia supply-chain rules [--json]
ozagia supply-chain explain RULE
```

- `check` builds the normal dependency inventory first, then runs the
  supply-chain rules. It reports `truth_assessed: false` and an explicit
  `not_assessed` list.
- `--fail-on high` exits 1 when a finding is high or critical. The default is
  `none`, so a report does not become a gate by accident.
- `ozagia-supply-chain` is the standalone entry point; both forms use the same
  code.
- The `dev_supply_chain` MCP tool exposes `check`, `rules` and `explain` to a
  coding agent. It is read-only and bounded like the CLI; a policy path must
  stay inside the workspace.
- The `dependencies` profile of `ozagia deep-review` runs the same checks, and
  accepts `--supply-chain-policy PATH`. Results appear in
  `08-dependencies.md`, `findings.json` and `inventory.supply_chain`.

## Rules

| Rule | Default severity | Detects |
|---|---|---|
| `untrusted_registry` | medium | A registry/index outside the allowlist, a non-default Composer repository, a Cargo source replacement, or a package resolved from such a source |
| `registry_credentials` | high | Tokens/passwords in `.npmrc`, `pip.conf`/`pip.ini`, URLs or lockfile sources (values are redacted) |
| `unsafe_tls` | high | Plain-HTTP registries, `strict-ssl=false`, `trusted-host`, Composer `secure-http=false` |
| `mutable_git_dependency` | medium | A git dependency without a full commit (branch/tag/HEAD or no revision) |
| `direct_source_dependency` | medium | A URL/path dependency where a registry release is expected |
| `wildcard_dependency` | medium | `*`, `latest`, `x` or unbounded zero ranges; a lockfile row without a resolved version |
| `missing_integrity` | medium | A lockfile with **mixed** integrity coverage (some rows hashed, some not), or any missing hash when the policy sets `require_integrity` |
| `install_script` | high | Dependency lifecycle scripts (`preinstall`/`install`/`postinstall`/`prepare`), custom `setup.py`, `setup.cfg` build commands, unknown Python build backends or `backend-path`, suspicious `build.rs` process/network logic, Composer install scripts/plugins |
| `typosquat_candidate` | medium | A declared name one edit (or a transposition) away from a popular package in the same ecosystem |
| `dependency_confusion_candidate` | high | A policy-declared internal package resolved from a public registry, or declared without a locked private source |

Every finding carries a stable key, locations, evidence, confidence, CWE,
recommendation and the global `not_assessed` list. Repeated findings aggregate
locations and an occurrence count. Sources (registry hosts, direct URLs and git
remotes) are represented by a short hash, not the raw value, so private
registries and credentials do not leak into reports.

## Policy

An explicit policy is a JSON object; no policy file is read implicitly.

```json
{
  "registries": [
    "https://registry.npmjs.org/",
    "https://pypi.org/simple",
    "https://index.crates.io/"
  ],
  "internal_packages": ["@acme/*", "acme-*"],
  "allow_git_hosts": ["github.com/acme"],
  "allow_install_scripts": ["esbuild", "sharp"],
  "allow_build_backends": ["acme_build.backend"],
  "allow_direct_sources": ["../vendor/*"],
  "require_integrity": true,
  "disabled_rules": ["wildcard_dependency"],
  "severity_overrides": {"typosquat_candidate": "high"}
}
```

- `registries` replaces the built-in public allowlist when present. Plain HTTP
  is never allowed.
- `internal_packages` uses glob patterns and drives the dependency-confusion
  check. A package matching an internal pattern is high when its lockfile
  source is public, and medium/low when no private lock source is present.
- `allow_install_scripts` suppresses install-script findings for reviewed
  packages.
- Policy loading rejects unknown keys, unknown rule IDs, invalid severities and
  non-HTTP(S) registries. The normalized policy is hashed
  (`sha256:…`) into the result and the deep-review plan hash.

## Coverage and limits

What is covered:

- npm (`package-lock.json`, `npm-shrinkwrap.json`), Yarn v1, pnpm (best effort),
  Cargo, uv/Poetry/PDM, Pipfile, Bundler, Composer and Gradle lock inputs;
- Python, Node, Rust, Go, Ruby and Composer manifests and registry settings;
- `node_modules/*/package.json` lifecycle scripts (bounded);
- `.npmrc`, `.yarnrc`, `pip.conf`, `pip.ini`, `.pypirc`, requirement index
  directives, `pyproject.toml` uv/Poetry sources, Cargo sources, Composer
  repositories/scripts/plugins, Gemfile sources and `go.mod` replacements.

Not assessed, explicitly:

- registry metadata, maintainers, ownership changes, yanks and malicious/
  deprecated package feeds;
- package tarball contents and runtime behaviour;
- signatures, attestations, transparency logs and SLSA provenance;
- CVEs and advisory ranges (use `--local-advisories` and the dependency
  scanners for those);
- dependency resolution beyond the committed lockfiles, including private
  registry policy semantics and build-time environment variables;
- generated/ignored files, vendored tarballs without metadata, and private
  packages whose names cannot be classified without a policy.

Typosquat detection is a heuristic over a small popular-name set; it is a
prompt to verify, not a verdict. `missing_integrity` intentionally reports
mixed coverage by default because hash formats vary; `require_integrity:
true` makes every missing hash a finding. The analyzer is bounded by files,
bytes, time and finding caps; truncation is reported in `not_checked`.

## Integration and prior art

The implementation borrows the evidence-first shape of the existing dependency
analyzer and the vocabulary of SLSA, OpenSSF Scorecard, OSV, Sigstore and
CycloneDX without claiming any of their guarantees. It does not download a
database and does not assign a compliance score.

Roadmap (separate work, all explicit opt-in):

- optional registry/OSV/provenance providers with network and hash pinning;
- attestation and Sigstore verification when artifacts are supplied locally;
- maintainer/ownership change signals from a pinned registry snapshot;
- package tarball inspection and install-script allowlisting with digests;
- resolved dependency-graph analysis beyond the lockfile rows.

## Tests

```bash
.venv/bin/python -m pytest -q tests/test_supply_chain.py
ozagia supply-chain rules
ozagia supply-chain check --json
```

`tests/test_supply_chain.py` covers policy validation, registry/credential
privacy, mixed-integrity detection, mutable/direct/wildcard sources, install
scripts and build backends, typosquat/confusion, allowlists, the CLI `fail-on`
gate and the dependency-analyzer integration.
