# Secrets by reference

Secrets are passed by reference, never by value. Configuration, job records,
CLI output, logs and model messages carry a `secret://...` reference. The
value is resolved only at the execution boundary: when a child environment or
process is about to receive it. Every resolved value is registered with a
bounded redactor for the lifetime of the process.

`ozagia.core.secrets` is standard-library only. It makes no network or model
calls, does not import a target project and does not read a value while
answering a status query (the keyring provider probes existence, but never
prints the value).

## Reference grammar

```text
secret://env/NAME
secret://file/NAME
secret://keyring/SERVICE/ACCOUNT
secret://pass/PATH
secret://op/VAULT/ITEM/FIELD
secret://session/TOKEN
```

| Provider | Path | Requirements |
| --- | --- | --- |
| `env` | one environment variable name | enabled by default |
| `file` | one file name under the secrets base | enabled by default; regular file, contained, size limit |
| `keyring` | service/account | optional `keyring` package; not enabled by default |
| `pass` | 1–8 path segments | `pass` executable; `allow_cli` must include `pass` |
| `op` | vault/item/field | `op` executable; `allow_cli` includes `op`; `allow_network = true` |
| `session` | one opaque token | an in-memory `SessionSecretStore` and matching harness/session |

Rules:

- `literal://` does not exist and plaintext values are not a fallback. A
  string that is not a valid reference is rejected, not treated as a value.
- Percent-encoding is decoded **before** validation. Encoded traversal
  (`%2E%2E`), encoded separators (`%2F`), control characters (`%00`) and
  invalid escapes are rejected.
- Names are bounded and ASCII. Path providers reject `.`, `..`, empty
  segments, backslashes, whitespace and absolute paths. A file reference names
  one file; `secret://file/a/b` is not a path.
- `parse_ref()` raises `SecretRefError` with a stable `code`:
  `invalid_ref`, `invalid_scheme`, `literal_scheme`, `invalid_provider`,
  `unknown_provider`, `invalid_segment`, `invalid_segment_count`,
  `invalid_encoding`, `invalid_control`, `invalid_traversal`, `absolute_path`
  or `invalid_length`. A message never contains a resolved value.

Absolute `file` references are refused unless `[secrets]
allow_absolute_files = true`; the reference form is
`secret://file//absolute/path`. This is a deliberate opt-out from the default
state-directory sandbox.

## Configuration

```toml
[secrets]
providers = ["env", "file"]          # subset: env, file, keyring, pass, op
allow_cli = []                       # subset: pass, op
allow_network = false                # required for op
allow_absolute_files = false         # permit secret://file//absolute/path
max_bytes = 4096                     # per file value, capped at 1 MiB
redact_output = true
```

`ozagia.core.secrets.load_config()` (or `SecretConfig.load()`) reads the table
from both project configuration files through
`ozagia.config.load_config_section`, so the legacy file
(`$OZAGIA_CONFIG`, `<root>/.ozagia.toml` or `<state>/config.toml`) wins per key
and `<root>/ozagia/config.toml` fills the rest. `[secrets]` is an owned
section: `ProjectConfig.unmapped` reports it as
`("secrets", "read by ozagia.core.secrets")` rather than treating it as an
index setting.

Unknown keys are reported in `SecretConfig.unknown` and `warnings`, never
applied. A malformed legacy file is forgiving: its values are skipped and the
versioned values or defaults apply, with the error in `errors`. An invalid
versioned value is also reported in `errors` (with the file path) and is not
applied. `max_bytes` is clamped to 1 MiB.

Environment overrides apply last:

| Variable | Effect |
| --- | --- |
| `OZAGIA_SECRET_PROVIDERS` | comma-separated provider subset |
| `OZAGIA_ALLOW_SECRET_NETWORK` | `true`/`false`/`1`/`0`/`yes`/`no`/`on`/`off` |
| `OZAGIA_SECRETS_DIR` | file base (absolute, or relative to the root) |

The file base defaults to `<state>/secrets`; it is not created by a read. A
relative `OZAGIA_SECRETS_DIR` is resolved under the root, and every relative
file reference is resolved, realpath-checked and confined to that base.
Symlinks that leave the base are denied; a symlink that stays inside the base
is allowed. A file must be a regular, non-empty UTF-8 file no larger than
`max_bytes`; one trailing newline (`\n` or `\r\n`) is stripped and nothing
else is trimmed. A group/world-accessible file is reported as a warning (mode
in `SecretValue.warnings` and `check()` detail) but is not refused.

## Resolution boundary

```python
from ozagia.core.secrets import SecretBroker

broker = SecretBroker(root)
secret = broker.resolve("secret://env/API_TOKEN")
child_env = broker.resolve_env({
    "API_TOKEN": "secret://env/API_TOKEN",
    "LOG_LEVEL": "info",
})
```

`SecretValue` masks itself in `str`, `repr` and `format`:
`SecretValue(secret://env/API_TOKEN)`. `reveal()` is the only accessor for the
value. Equality and hashing use the reference, never the value. `ResolvedEnv`
has `values` (the actual strings for the child), `refs` (key → `SecretRef`),
`plain_keys` and the broker's `redactor`; its `repr` shows counts only.

A plain environment value is allowed for non-secret settings; it is reported
in `plain_keys` and is not auto-detected or logged. Secret references are the
supported path for credentials.

`resolve_env()` is the boundary. Do not call `reveal()` while building a job
record, a log line, a prompt or CLI output.

### Redaction

`SecretBroker.redactor` (a `Redactor`) keeps at most 64 resolved values and
replaces exact, JSON-escaped, URL-encoded (with and without `/`), shell-quoted
and base64 forms in text. Patterns are sorted longest-first so a short secret
cannot leave part of a longer one behind. Values shorter than 8 characters,
all-whitespace values, single-character repetitions and a small list of
trivial words are never registered, and the replacement is never empty.
`redact_structure()` walks dicts, lists and tuples (including keys) with a
depth bound. `contains()` tests the exact registered value.

This is defense in depth, not a guarantee. It only knows values this process
resolved; values from another process, an undetected encoding, or text that
never passed through the redactor are not covered.

### Status without values

`broker.check(ref)` returns a status string (`available`, `missing`, `denied`,
`provider_unavailable`, `invalid` or `error`) with `reference`, `provider`,
`code` and `detail`. It never returns or registers the value. `check_many()`
checks several references. Env and file checks inspect existence/size only;
`pass` and `op` report that the provider is ready without probing the value
(probing would run a command whose output may be the secret); keyring probes
`get_password()` and discards the result. `providers()` and `describe()` report
names, availability, requirements, notes and counts only.

## Session-scoped captures

Text pasted into a chat is not a secret store. `SessionSecretStore` can hold a
pasted value **in memory only** for the life of the process:

```python
from ozagia.core.secrets import SessionSecretStore, detect_text

store = SessionSecretStore()
captured = store.capture(
    pasted_value, harness="opencode", session="ses_123", label="api token",
    ttl_s=3600)
print(captured.ref)  # secret://session/<opaque token>, safe to keep

rewritten = detect_text(
    pasted_text,
    ref_factory=lambda value, label: store.capture(
        value, harness="opencode", session="ses_123", label=label))
```

- A capture is scoped by `(harness, session)` and returns a random opaque
  `secret://session/<token>` reference, a `sha256:<16 hex>` fingerprint, the
  optional label, creation time and expiry. The token is not derived from the
  value. `CapturedSecret` has no value field and its `repr`/`as_dict()` show
  none.
- Defaults: 32 entries per session (oldest evicted first), 16 KiB per value,
  one-hour TTL (capped at 24 hours) and 64 sessions (oldest session evicted).
  Expired entries are pruned on access. `list()` returns metadata,
  `forget(ref, ...)` removes one, `clear(harness, session)` removes a session
  and `prune()` removes expired entries. Nothing is written to disk.
- `resolve(ref, harness=..., session=...)` succeeds only for the matching
  session and an unexpired token. A wrong session is denied
  (`session_mismatch`); no context is denied (`session_context_required`);
  expiry is `missing` with code `expired`. `SecretBroker.resolve()` accepts
  the same optional `harness`/`session`; session values are never cached, so
  expiry and `forget()` take effect on the next resolution. Resolved session
  values are registered with the broker redactor like any other value.
- The session provider is not a `[secrets] providers` entry. It needs a live
  `SessionSecretStore`; without harness/session context the broker denies the
  reference. The store's `repr` and `describe()` expose counts and limits, not
  tokens or values.

The daemon/plugin wiring is the next slice: capture on the host hook, register
the `SessionSecretStore` with the daemon, pass the session identity to job and
child-process boundaries, and clear the store when the session ends. This
slice deliberately has **no CLI `capture`/`resolve` command**: a CLI process
cannot persist an in-memory store for a later process, and writing the value
to disk would defeat the boundary.

### Pasted-text detection

`detect_text(text, max_secrets=16, ref_factory=...)` reuses the existing
security module's credential patterns and its quoted `key = "value"`
assignment rule. It detects common token shapes (AWS, GitHub, Anthropic,
OpenAI, Slack, Google, JWTs, private-key blocks, URL credentials) and
credential-word assignments only when the value looks opaque (at least 12
characters, mixed classes, sufficient entropy). Absolute minimum length is 8.
It leaves placeholders, obvious test canaries, short values and existing
`secret://...` references alone.

Each detection is replaced by the caller's reference (or `[REDACTED]` when no
factory is supplied, the cap is reached, or the factory fails). `TextCapture`
returns the rewritten text, value-free metadata (fingerprint, label/key,
start/end, ref), `detected`, `captured`, `withheld`, `truncated` and
`not_assessed`. `max_secrets` bounds captured metadata; detections beyond it
are still redacted. Text longer than 512 KiB is scanned only to that boundary
and flagged `truncated`.

This is conservative and best effort. It can miss secrets that do not match a
pattern, are encoded, are split across lines, or are already stored/sent by
the host before the hook. A false positive is a redacted value or an
unnecessary capture; a false negative is not detected. Review
`not_assessed` and treat the rewritten text as safer, not guaranteed clean.

## CLI

```bash
PYTHONPATH=src python -m ozagia.secrets_cli providers --root .
PYTHONPATH=src python -m ozagia.secrets_cli list --root . --json
PYTHONPATH=src python -m ozagia.secrets_cli check secret://env/API_TOKEN
PYTHONPATH=src python -m ozagia.secrets_cli doctor --root .
```

`providers` prints names, whether they are enabled/available/ready, and their
requirements. `list` finds `secret://` strings in both configuration files
(dotted key and source path) and reports their status. `check` accepts one or
more references and reports status only: exit `0` when all are available, `1`
when any is missing/denied/unavailable/failed, and `2` when any is invalid or
for usage errors. `doctor` shows the configuration source, file base,
providers, errors and session-store counts. No command prints a resolved value
and there is no `resolve`/`show`/`capture` command.

## Rotation and revocation

- **`env`**: change the daemon's environment and restart/reload it; a cached
  value persists until `broker.clear_cache()` or process restart.
- **`file`**: replace the file atomically. A resolved value is cached like the
  other providers until `broker.clear_cache()` or process restart; the next
  resolution after a clear reads the new file. Prefer mode `0600`; a
  group/world-readable file is warned about.
- **`keyring` / `pass` / `op`**: rotate in the external store. Resolved values
  are cached for up to 64 references; call `clear_cache()` or restart. `op`
  also requires `allow_network` and a fresh CLI session.
- **`session`**: `forget(ref, harness=..., session=...)` revokes one capture;
  `clear(harness, session)` ends a session; TTL expiry is automatic. An
  old reference is no longer resolvable after any of these.
- Deleting a value from configuration is not rotation. Remove the old
  credential at the provider, then update the reference.

## Threat caveats

- A resolved value lives in process memory. A process with the same
  privileges, a core dump, swap or a debugger can read it. Do not log it or
  put it in an exception message.
- `env` values are visible to same-user processes that can inspect the
  environment (`/proc/PID/environ`, process listings on some systems). Prefer
  `file`, `keyring`, `pass` or `op` where the threat model includes other
  local users.
- `file` references are confined to the base by realpath and regular-file
  checks. A writable secrets directory lets an attacker substitute a value;
  keep it mode `0700` and owned by the daemon user. The optional
  `allow_absolute_files` flag removes the base containment and must be used
  deliberately.
- `pass`/`op` run a fixed argv with `shell=False`, a sanitized environment,
  `stdin=DEVNULL`, a timeout (5 s and 10 s) and captured output. Their
  stdout/stderr and exit text are never copied into an error or report. They
  are still external programs with their own trust model; a compromised
  binary or agent can leak the value.
- `op` requires `allow_network = true`; the CLI may contact 1Password. No test
  uses a real store, network, `pass` or `op`.
- `keyring` is an optional package with platform-specific backends. A headless
  server may have no usable backend; it is reported as
  `provider_unavailable`, not installed or unlocked automatically. GUI
  keychains and sysadmin-managed stores vary by host.
- Session capture is memory-only. The host's chat transcript, terminal
  scrollback, shell history, model provider logs and network traffic may
  already contain the pasted value before detection runs. The hook can only
  rewrite text it receives. It does not guarantee that the host, a model
  provider or an external service forgets it.
- This slice does not yet wire runtime services or jobs to the broker. Until
  that lands, a job record or model message must carry references and the
  runtime must resolve them at spawn; do not add a plaintext path.
- A clean `check` means the provider is reachable, not that the credential is
  valid, unexpired or authorized. Missing/denied statuses are the safe
  outcomes.

## Tests

```bash
PYTHONPATH=src python -m pytest -q tests/test_secrets.py \
  tests/test_secrets_cli.py tests/test_security.py tests/test_config.py
.venv/bin/ruff check src tests
```

The tests use temporary directories and injected fakes for keyring, `pass`
and `op`. No test uses a real secret store, the network, `pass`, `op` or the
keyring package.
