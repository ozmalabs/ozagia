# Security and antipattern scan

`ozagia.core.security` is an offline triage layer. It reads an **existing**
`.ozagia/graph.json`, the working tree, dependency manifests and an optional
fixed registry of external tools. It does not build or refresh the graph, run
tests, install a tool, call a model, edit the repository, or use the network by
default.

A finding is evidence for review. It is not a proof that a path is reachable or
exploitable, and a clean scan is not a statement that the code is secure.
Every report and finding has `truth_assessed: false`.

## Commands

```bash
PYTHONPATH=src python -m ozagia.security_cli scan --root . --json
PYTHONPATH=src python -m ozagia.security_cli scan --root . \
  --tool bandit --tool gitleaks --fail-on high
PYTHONPATH=src python -m ozagia.security_cli scan --root . \
  --staged --json
PYTHONPATH=src python -m ozagia.security_cli tools
PYTHONPATH=src python -m ozagia.security_cli explain sec-0123456789ab
PYTHONPATH=src python -m ozagia.security_cli explain sec-0123456789ab \
  --report security-report.json
PYTHONPATH=src python -m ozagia.security_cli secrets --root . --staged
PYTHONPATH=src python -m ozagia.security_cli secrets --root . \
  --diff main..HEAD --json
```

`scan` exits `0` after a completed report (findings are data), `1` when a
finding meets `--fail-on` (default `high` from `[security]`), and `2` for a
usage or IO error. `secrets` exits `0` when the gate passes or
`block_secrets = false`, `1` when a secret is found and would block, and `2`
for a usage or IO error. Only an explicit `--out` writes a file.

`scan --staged` and `scan --diff BASE..HEAD` add a scoped secret report to the
normal scan. The standalone `secrets` command is the same redacted scan
without the graph, dependency and tool work.

## Configuration

```toml
[security]
enabled = true
tools = ["semgrep", "bandit", "gitleaks"]   # explicit opt-in
allow_network = false
fail_on = "high"          # blocking|high|medium|low|never
exclude = ["tests/**", "vendor/**", "docs/**"]
max_findings = 200

block_secrets = true                       # default commit/push gate
secret_allowlist = [
  "path:tests/fixtures/**",
  "rule:generic_high_entropy",
  "sha256:0123abcd",                       # reviewed fingerprint
]
```

`enabled = false` is reported but does not turn a direct CLI invocation into
silence; the caller asked for the scan. `block_secrets` is the only global
switch for the commit/push gate.

### Precedence and files

The same table is read from two files through
`ozagia.config.load_config_section`:

1. the legacy file: `$OZAGIA_CONFIG`, `<root>/.ozagia.toml` or the state
   directory's `config.toml` (highest priority);
2. the versioned `<root>/ozagia/config.toml`.

The legacy file wins per key; the versioned file supplies only the keys the
legacy file leaves unset. Every key shown above is supported in either file:

```toml
# ozagia/config.toml: used for the keys .ozagia.toml does not define
[security]
enabled = true
tools = ["bandit"]
exclude = ["tests/**", "vendor/**"]
block_secrets = true
secret_allowlist = ["path:tests/fixtures/**"]
```

`ozagia.core.security.load_config(root)` returns the effective values plus
`sources` (per key: `legacy`, `repo` or `default`), `files` (both paths),
`error`/`errors`, and `legacy_error`/`repo_error`. A malformed legacy file
stays forgiving: its values are skipped and no exception is raised. A
malformed or invalid versioned file is reported rather than silently ignored;
`load_config_strict(root)` raises `ConfigError`. A scan still completes and
includes the error in `config.error`, `config.errors` and its warnings. Values
from the versioned file are type-checked like the registry (booleans, string
lists, `fail_on`, and an integer `max_findings`, capped at 1000). A legacy
value with the wrong type is ignored, as before. Unknown or nested `[security]`
keys are ignored by the scan and reported in `ProjectConfig.unmapped` and by
`ozagia doctor`.

`ozagia doctor` text names both configuration paths, the active source, the
precedence and a bounded unmapped count (for example `unmapped=20 of 25…`); it
never prints setting values or secrets. `ozagia.adapters.registry` keys its
reload cache on both files (path, `mtime_ns`, size and content digest), so
editing the non-active `ozagia/config.toml` invalidates it; `reload=True` still
forces a rebuild.

Secret references (`secret://...`), provider policy and in-memory session
captures are a separate layer; see [secrets.md](secrets.md). The security scan
still does not read an external secret store and never resolves a reference.

## Graph-derived rules

The scan loads `.ozagia/graph.json` read-only. A missing, stale or unverified
snapshot is reported; filesystem, manifest and secret checks still run. A
stale snapshot is not used for route/sink graph paths. Graph symbols provide
line ranges, decorators and recorded `calls` edges; a path can therefore be
reported as:

```text
route:POST /run -> sym:src/app.py::run -> sink:shell
```

Rules are per-line pattern checks plus a few contextual checks. They include:

| Area | Examples | OWASP / CWE |
| --- | --- | --- |
| Access control | route/handler with no visible auth/permission hint; private routes such as health/login are skipped | A01, CWE-862 |
| Crypto | MD5/SHA-1, `verify=False`, `CERT_NONE`, `rejectUnauthorized: false`, static IV/nonce, hardcoded key | A02, CWE-295/327/329/321 |
| Injection | `eval`, `exec`, `shell=True`, `os.system`, f-string/format SQL, template string, path traversal, archive extraction | A03, CWE-95/78/89/1336/22 |
| Insecure design | non-cryptographic randomness for a security value, `tempfile.mktemp` | A04, CWE-338/377 |
| Misconfiguration | `debug=True`, wildcard CORS, world-writable mode, default credentials, Flask without visible header middleware | A05, CWE-489/942/732/1392 |
| Components | dependency inventory from `pyproject.toml`, requirements, `package-lock.json`, `Cargo.lock`, `go.mod`; no CVEs are invented | A06, CWE-1104 |
| Auth | JWT `alg: none`/unverified decode, cookie flags, weak password hashing | A07, CWE-347/614/916 |
| Integrity | unsafe YAML/pickle/marshal, `curl | sh`, disabled TLS verification for installs, unpinned CI actions, `pull_request_target` with head checkout, `write-all`, `--no-verify` | A08, CWE-502/494/829/250 |
| Logging | secret-like values passed to a logger/print/console call | A09, CWE-532 |
| SSRF | request-controlled value reaching `requests`/`httpx`/`urlopen`/`fetch` without a visible allowlist | A10, CWE-918 |

A graph path is attached when the matched line falls inside a graph symbol. If
a route handler calls a helper through a recorded `calls` edge, the path
includes the intermediate symbol. No runtime data-flow analysis is performed.

## Secrets

Provider formats, generic high-entropy assignments and credential-bearing
paths are detected:

- AWS access key IDs, GitHub tokens, OpenAI keys, Anthropic keys, Slack
  tokens/webhooks, Google API keys/OAuth secrets, JWTs, private-key blocks,
  URL credentials;
- quoted or bare credential assignments whose value passes an entropy check;
- `.env`/`.env.*`, `.npmrc`, `.pypirc`, `credentials`, SSH private-key names,
  `.aws/credentials`, gcloud application-default credentials, Docker/Kube
  config, and key-store suffixes (`.p12`, `.pfx`, `.jks`, `.keystore`, `.key`).

The value is **never** written to a finding, report, warning, event, audit
record or CLI output. A finding contains `rule`, `path`, `line`, a
`secret_fingerprint` of the form `sha256:<16 hex>` and
`value_withheld: true`. The fingerprint is a one-way hash, not the secret.
`redact_text()` is applied to report JSON, text output and audit text as
defense in depth.

The staged/range scan reads `git diff --unified=0`, so only added lines are
checked. It is bounded by 1 MiB per file and 16 MiB of diff text. Binary files
and larger files are listed in `skipped` with an explicit reason; the file
path is still checked for a credential-file rule. A truncated diff is marked
`summary.truncated` and the commit/push gate treats it as not checked.

## Default commit and push gate

`ozagia.core.worktree.policy_check` runs the secret gate by default:

- `kind="commit"` scans staged content and reports `commit.secret` errors;
- `kind="push"` scans the outgoing `base..HEAD` diff and reports
  `push.secret` errors;
- `block_secrets = false` still scans and reports the count, but adds
  `commit.secret_gate` / `push.secret_gate` warnings and does not block. The
  report's `security.disabled` field says the gate is off.

Allowlist entries are matched by path glob, exact rule name, or fingerprint
(prefix match after `sha256:`):

```toml
[security]
secret_allowlist = [
  "path:tests/fixtures/**",
  "rule:generic_high_entropy",
  "sha256:0123abcd",
]
```

A one-time override takes a confirmed fingerprint token:

```bash
ozagia-worktree policy --for commit --allow-secret sha256:0123abcd
python -m ozagia.security_cli secrets --root . --staged \
  --allow-secret sha256:0123abcd
```

The override applies only to that invocation. It is recorded in
`.ozagia/security/audit.jsonl` as a redacted decision with rule, path, line,
mode and fingerprint. The secret value is not stored. An invalid token (not a
`sha256:` fingerprint) is reported and ignored.

## Optional tools

The registry is fixed. `tools()` reports availability with `shutil.which`;
nothing is installed or downloaded.

| Tool | Kind | Network required | Default command |
| --- | --- | --- | --- |
| `pip-audit` | dependency | yes | `pip-audit --format json` |
| `npm` | dependency | yes | `npm audit --json` |
| `cargo` | dependency | yes | `cargo audit --json` |
| `govulncheck` | dependency | yes | `govulncheck -json ./...` |
| `osv-scanner` | dependency | yes | `osv-scanner --format json -r .` |
| `semgrep` | SAST | yes (`--config auto`) | `semgrep scan --sarif --quiet --config auto .` |
| `bandit` | SAST | no | `bandit -f json -r .` |
| `gitleaks` | secret | no | `gitleaks detect --no-git --redact --report-format json --report-path -` |
| `trivy` | dependency/secret | yes | `trivy fs --format json --quiet --scanners vuln,secret .` |

A network-requiring tool is refused unless `allow_network` is true (CLI:
`--allow-network`, or `[security] allow_network = true`). A missing tool is
`unavailable`, not a failure. Each adapter uses `shell=False`, a timeout, an
output cap and a parser for JSON, SARIF, OSV or the tool's native shape. The
report records tool name, version, command, exit status and the database time
when the output supplies one (`database.status = "unreported"` otherwise).
Tool findings carry the same `source.tool` provenance and no raw stdout.

An injected `runner(argv, timeout=..., cwd=...)` may be supplied by a caller
or test. It is not a model and not a shell; the fixed registry and the network
gate still apply.

## Dependency inventory

The inventory records manifests, component counts, exact/unknown versions and
whether a lockfile was detected. Without a dependency scanner, CVE status is
`not_assessed`; a low-severity inventory finding says so explicitly. With a
scanner, its results are recorded but the inventory is still
`scanner_reported_not_exhaustive`, never "clean".

Unknown versions, lockfile gaps and unavailable scanners are explicit. The
scan does not resolve a package registry or guess a CVE.

## Finding shape

```json
{
  "id": "sec-0123456789ab",
  "category": "owasp:a03",
  "kind": "command-injection",
  "severity": "high",
  "confidence": "medium",
  "file": "src/app.py",
  "line": 42,
  "graph_path": ["route:POST /run", "sym:src/app.py::run", "sink:shell"],
  "message": "subprocess call enables a shell",
  "evidence": {"kind": "graph_path", "detail": "...", "path": ["..."]},
  "source": {"type": "graph", "rule": "shell-true"},
  "remediation": "use an argv list and shell=False",
  "false_positive_note": "...",
  "not_assessed": ["runtime reachability", "input validation outside this file"],
  "cwe": ["cwe:78"],
  "owasp": ["a03"],
  "truth_assessed": false
}
```

Secret findings additionally contain `secret_rule`, `secret_fingerprint` and
`value_withheld: true`. Dependency inventory findings contain `cve_status`.
Tool findings contain `source.tool`, `source.version`, `source.command`,
`source.exit_code` and `source.database`.

Findings are deduplicated by rule/target, capped by `max_findings`, sorted by
severity, confidence, file, line and rule, and assigned stable `sec-...` ids.
The same input produces the same JSON.

## Integration

- **Code review:** pass an existing security report as
  `review_diff(..., security_report=report)`. The packet gets a `security`
  section with the same finding ids, `truth_assessed: false`, and a
  `not_assessed` entry. The review does not run a scan or a tool.
  `exit_code` maps critical to blocking, high to major, medium to minor and
  low/info to note.
- **Repo scan:** pass `security_report=...`, or place the report at
  `.ozagia/security/latest.json`; the scan reads it and ranks a `security`
  category. It never runs a security tool itself. Without a report the
  category contains a low-ranked "security has not been assessed" gap.
- **Hooks:** `ozagia hooks install --pre-commit --pre-push` installs an
  opt-in wrapper that calls the same `policy_check` gate. `ozagia hooks
  uninstall` removes only a hook with the ozagia marker, and `ozagia hooks
  status` reports ownership. There is no automatic installation.
- **Bounded tasks/UI/MCP:** future callers use the same JSON; no auto-fix,
  exploit generation or hosted database is in this slice.

## Hooks

```bash
ozagia hooks install --pre-commit --pre-push   # explicit, reversible
ozagia hooks status
ozagia hooks uninstall --pre-commit
```

The wrapper calls `python -m ozagia.worktree_cli policy --for commit` (or
`--for push`) and exits nonzero on a policy error. It prints nothing on
success and a concise, redacted report on failure; secret values are never
printed. The interpreter is the one running ozagia, or `--python`, or
`$OZAGIA_HOOK_PYTHON`.

An existing hook without the `# ozagia security gate` marker is never
overwritten unless `--force` is passed. `uninstall` keeps a foreign hook.
Installation is always explicit: no policy check, scan or preview installs a
hook. The hooks are shell wrappers, not a second policy implementation. The
Python API is `ozagia.core.hooks.install`, `uninstall` and `status`;
`ozagia.core.security.install_hook` remains for existing callers.

## Limits and non-goals

- No DAST, exploit generation, auto-fix or auto-install.
- No proof of reachability, exploitability or data flow; graph paths are
  syntactic and may be stale.
- No secret history beyond staged content or the requested diff range; no
  binary or large-file content; no external secret store.
- No CVE claim without a scanner, and no claim that a scanner result is
  complete.
- Tool commands are fixed. The scan never runs arbitrary argv or a shell.
- A clean scan does not mean the code is secure.

## Tests

```bash
PYTHONPATH=src python -m pytest -q tests/test_security.py tests/test_worktree.py \
  tests/test_code_review.py tests/test_repo_scan.py
.venv/bin/ruff check src tests
```

The tests cover graph sink paths, secret redaction/fingerprinting, hardcoded
keys/TLS, dependency inventory, injected tool provenance, unavailable tools,
the network gate, dedupe/caps, CLI exit codes, the staged commit gate, push
range gate, allowlists, one-time overrides, disabled gates, binary/large-file
skips, legacy/versioned `[security]` precedence and malformed-file handling,
and code-review/repo-scan integration. No test uses the network or a model.
