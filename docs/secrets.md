# Secrets by reference

Secrets are passed by reference, never by value. Configuration, job records,
CLI output, logs and model messages carry a `secret://...` reference. The
value is resolved only at the execution boundary: when a child environment or
process is about to receive it, or when a daemon session capture is explicitly
needed. Every resolved value is registered with a bounded redactor for the
lifetime of the process.

Runtime services (`[runtime.services.NAME.env]`), background jobs
(`job_start` with an `env` value), and the daemon's `secrets` op all use this
boundary. A resolved value is not written to a record, a log file, a journal,
a CLI output or a daemon response by ozagia. The guarantee is about values
this process resolved; a value printed by a child in a form the redactor does
not know, or a value in a file another process wrote, is outside it.

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

### Runtime services

```toml
[runtime.services.api]
command = ["python", "-m", "app"]
workdir = "services/api"
env = { LOG_LEVEL = "info", API_TOKEN = "secret://env/API_TOKEN" }
```

`[runtime.services.NAME.env]` is an optional table of environment names to
strings. Names are bounded ASCII identifiers. A value that starts with
`secret://` is kept as a reference and resolved by `ServiceManager` at spawn
with `harness="runtime", session=None`. A plain value is allowed only for a
non-sensitive name; a credential-like name (`token`, `password`, `secret`,
`api_key`, `authorization`, ... from the existing `_SENSITIVE_KEY` words) must
use a reference, or configuration fails with an error naming the service and
key.
Empty values, control characters and malformed references are rejected at
configuration load; nothing is resolved then.

- The child receives the manager's inherited environment plus the resolved
  entries (`env={**os.environ, **resolved}`). A service without an `env` table
  keeps the old inherited-environment behavior exactly.
- `secret://session/...` is **not valid for a runtime service**: there is no
  interactive session at spawn, so it is denied with
  `session_context_required`. Start a job in the session that captured the
  value instead, or use `env`/`file`/`keyring`/`pass`/`op`.
- Runtime records contain no environment values or refs (they carry only a
  configuration fingerprint that includes the configured env refs). Foreground
  `output` and background log chunks are redacted before they are returned or
  appended to `logs/<id>.log`; a value spanning a line boundary is handled by
  line buffering, and output draining never raises.
- Start/run errors name the service and the configuration key/code, never a
  value. Provider output and argv values are not copied into an error.

### Background jobs

`job_start` (daemon op, `ozagia bg run --env NAME=VALUE`, and the MCP
`job_start` tool) accepts environment additions. A `secret://` value is
resolved by the daemon's `SecretBroker` with the job's `harness` and `session`:

```bash
ozagia bg run --env API_TOKEN='secret://env/API_TOKEN' -- ./serve.sh
```

- A session reference resolves only for the job's own `(harness, session)`.
  Another session is denied (`session_mismatch`); no harness/session is denied
  (`session_context_required`). The refusal is structured
  (`env references could not be resolved (<code>): KEYS`) and contains no
  value.
- The job record stores `env_keys` (names) and `env_refs` (the reference
  strings), never values. `jobs.json` is local state; the refs are safe to
  keep there.
- The child's stdout/stderr is read through a bounded pump that decodes
  incrementally, holds partial lines, and redacts **before** writing
  `<state>/jobs/<id>.log`. The pump is joined when the process group is empty,
  before the job is reported as ended. `job_log`, `job_wait` output and
  `log:` regex matches are redacted again on read, so a legacy log written
  before the pump cannot leak a value this daemon has resolved. Cursors remain
  byte offsets into the redacted log; a partial last line is never split or
  duplicated and is written when its newline arrives or the job ends.
- A value that never matches a registered value (another process's log, an
  encoding the redactor does not generate, or output written before the daemon
  resolved it) is not guaranteed to be removed.

### Daemon and plugin hook

The daemon owns one `SecretBroker` and one memory-only `SessionSecretStore`
per root. The `secrets` op is bounded and value-free:

| Action | Input | Result |
| --- | --- | --- |
| `capture_text` | `text`, `harness`, `session` | `detect_text` rewrite with session refs; detected/captured/withheld counts, fingerprints, creation/expiry |
| `capture` | `value`, optional `label`, `ttl_s`, `harness`, `session` | one session ref, fingerprint, creation/expiry |
| `list` | `harness`, `session` | that session's refs and metadata only |
| `forget` | `ref`, `harness`, `session` | whether one capture was removed |
| `providers` | none | provider names, availability, requirements |
| `check` | `refs`, optional context | status only (`available`, `missing`, ...) |

There is deliberately **no** `resolve`/`show` action. `capture` and
`capture_text` are called with the value/text over the local daemon socket
(the CLI reads stdin; a plugin hook passes text it received). No value is
returned. Captured values are registered with the broker redactor, so a
response that echoes one is redacted before `_Handler._send` and before the
journal record is written. The security module's generic redaction runs as
defense in depth; `secret://` references are protected from it. `session_event
end` clears that session's captures, and expired entries are pruned on access.
`stats` reports `secret_captures`, `secret_checks`, `secret_redactions`,
`secret_errors` and `secret_store` counts. Any client that can write to the
daemon socket can call these actions; protect the socket and state directory
like the rest of `.ozagia/`.

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
never passed through the redactor are not covered. Runtime logs, job logs and
daemon responses apply the redactor unconditionally at their boundary; the
`redact_output` setting only governs other reports. A resolved value must never
be written even when report redaction is turned off.

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

The daemon/plugin wiring is the current slice: the daemon owns the store, the
`secrets` op captures text and values over its local socket, jobs resolve
session references only for their own harness/session, and a session end
clears its captures and request records. The CLI's `capture`/`capture-text`
commands read from stdin and call the daemon; they never take a value in argv.
No in-memory store is persisted by a CLI process, and a capture made by one
session cannot be resolved by another.

### Model-initiated secret requests

A model can ask for a named secret without receiving its value. This slice is
host-agnostic: the OpenCode plugin exposes `ozagia_secret_request` when the
host provides the native tool registry and permission API (the same path as
`ozagia_structure`), and any tool or person can create the same request with
the CLI. MCP is unchanged because an MCP server does not learn the harness
session id; accepting one from model arguments would let a caller claim
another session.

```json
{"action": "request", "harness": "opencode", "session": "ses_123",
 "label": "OpenAI API key", "purpose": "the task calls the OpenAI API",
 "target": "session", "ttl_s": 300}
```

The daemon records metadata only and returns an opaque `request_id`, `state`
and `expires`. A request has no value, prompt text or source path. The model
then tells the user to run the trusted local CLI:

```bash
ozagia-secrets fulfill req_<handle> --harness opencode --session ses_123 --root .
```

`fulfill` reads one value with `getpass` (or `--stdin` for tests and
automation), sends it to the local daemon, captures it in the session store,
and prints only the reference, fingerprint and expiry. The next status call
for the same request returns `fulfilled` with `ref` and `fingerprint`; the
model receives those and never the value.

Protocol actions (all require matching `harness`/`session` for
`request_status`, `request_fulfill` and `request_decline`):

| Action | Input | Result |
| --- | --- | --- |
| `request` | `label`, optional `purpose`, `target: "session"`, `ttl_s` | pending record: `request_id`, `state`, `expires` |
| `request_list` | none | that session's records, newest last |
| `request_status` | `request_id` | state; `ref`/`fingerprint` only when fulfilled |
| `request_fulfill` | `request_id`, `value` | fulfilled record: `ref`, `fingerprint`, expiry |
| `request_decline` | `request_id` | declined record, no ref |

Lifecycle rules:

- pending requests expire after 300 s by default, capped at 600 s; at most 16
  are pending per session and 64 records are kept (terminal ones evicted
  first). A duplicate `request` with the same label, purpose and target
  returns the existing record instead of creating another.
- `fulfill` is idempotent: fulfilling an already fulfilled request returns the
  same reference and ignores the new value. Fulfilling or declining a
  declined/expired request is `request_closed`.
- A cross-session `request_status`, `request_fulfill` or `request_decline` is
  `request_mismatch` and returns no reference. A request id is otherwise
  unguessable and never derived from the value.
- Declined and expired records carry no reference. If a fulfilled capture is
  forgotten, expires or the session ends, later status marks the request
  expired and drops its reference; the fingerprint remains for rotation.
- A plugin tool without session context returns a typed refusal
  (`session_unavailable`); a daemon refusal is `refused` and an unreachable
  daemon is `unavailable`. None includes a value.

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

# Session captures (the value/text is read from stdin, never argv).
PYTHONPATH=src python -m ozagia.secrets_cli capture --session ses_123 \
  --label "api token" --ttl 600 < token.txt
PYTHONPATH=src python -m ozagia.secrets_cli capture-text --session ses_123 \
  < prompt.txt
PYTHONPATH=src python -m ozagia.secrets_cli list --session ses_123 \
  --harness opencode
PYTHONPATH=src python -m ozagia.secrets_cli check secret://session/... \
  --session ses_123
PYTHONPATH=src python -m ozagia.secrets_cli forget secret://session/... \
  --session ses_123

# Model-initiated requests: no value is read or printed by `request`.
PYTHONPATH=src python -m ozagia.secrets_cli request --label "OpenAI API key" \
  --purpose "the task calls the OpenAI API" --harness opencode --session ses_123
PYTHONPATH=src python -m ozagia.secrets_cli requests --harness opencode \
  --session ses_123
PYTHONPATH=src python -m ozagia.secrets_cli fulfill req_<handle> \
  --harness opencode --session ses_123
PYTHONPATH=src python -m ozagia.secrets_cli decline req_<handle> \
  --harness opencode --session ses_123
```

`providers` prints names, whether they are enabled/available/ready, and their
requirements. `list` without `--session` finds `secret://` strings in both
configuration files (dotted key and source path) and reports their status;
`list --session` asks the daemon for that session's capture metadata. `check`
accepts one or more references and reports status only: exit `0` when all are
available, `1` when any is missing/denied/unavailable/failed, and `2` when any
is invalid or for usage errors. With `--session`, `check` uses the daemon so a
session reference can be checked in its own context. `doctor` shows the
configuration source, file base, providers, errors and session-store counts.

`capture` reads one value from stdin (one trailing line ending is trimmed) and
prints only its reference, fingerprint and expiry. `capture-text` reads text
from stdin and prints the rewritten text (or JSON with refs and counts). If no
daemon answers, `capture-text` **fails closed**: it prints the text with
detected secrets replaced by `[REDACTED]`, writes the daemon error to stderr,
and exits `2`; no value is captured. No command prints a resolved value and
there is no `resolve`/`show` command.

`request` creates a pending request and prints its id, state, expiry and the
`fulfill` command; it never accepts or prints a value. `requests` lists the
session's records (pending and terminal), and `decline` records a terminal
decline. `fulfill` prompts with `getpass`, or reads one value from stdin with
`--stdin` for tests and automation, and prints only the reference,
fingerprint and expiry. It exits `0` on success, `1` for a declined, expired,
closed or cross-session request, and `2` when the daemon is unavailable or the
input is empty. A value is never accepted in argv; there is no reveal command.

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
- **`session`**: `forget(ref, harness=..., session=...)` (or
  `ozagia.secrets_cli forget`) revokes one capture; `clear(harness, session)`
  ends a session and `session_event end` clears it; TTL expiry is automatic.
  An old reference is no longer resolvable after any of these. Runtime
  services cannot resolve a session reference at all.
- **Runtime services and jobs**: a restart (or a new job) resolves the
  reference again. The daemon caches provider values; `clear_cache()` or a
  daemon restart is needed after rotation for a cached provider. A child
  process keeps the value it received until it exits.
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
- Runtime services and jobs inherit the daemon's (or manager's) environment
  plus resolved entries. The child and any same-user process that can read
  `/proc/<pid>/environ` or process memory can see the value. Use `file`,
  `keyring`, `pass` or `op` when other local users are in scope. Core dumps,
  swap, a debugger or a compromised child can read it; ozagia cannot prevent
  that.
- Logs are redacted at write time only for values that process has resolved.
  A legacy log, another process's output, a binary or encoded form the
  redactor does not generate, or a value resolved after the output was written
  may remain. The daemon journal stores bounded reasons and metadata, not job
  output; job logs live under `.ozagia/jobs/` and runtime logs under
  `.ozagia/runtime/logs/` with restrictive modes. Protect the state directory.
- A clean `check` means the provider is reachable, not that the credential is
  valid, unexpired or authorized. Missing/denied statuses are the safe
  outcomes.
- The daemon accepts `capture`, `request_fulfill` and `request_decline` from a
  same-user local client; it cannot verify which process sent the value. The
  trusted-CLI rule is a client convention, not a cryptographic boundary.
  Any process that can talk to the daemon socket already shares the daemon
  user's privileges. The model-facing plugin tool cannot send a value, and the
  MCP `dev_secrets` tool stays read-only.
- The model-initiated request slice has a metadata-only handle and a CLI
  prompt. It does **not** yet include the OpenCode native secret dialog,
  composer pill, prompt-paste rewrite, keychain/`pass`/`op` persistence UI,
  or live-host verification. A user who pastes a value into chat still relies
  on the host hook and the best-effort detector described above.
- No live GUI or host plugin is implemented here: the hook path is the daemon
  `secrets capture_text` op, exercised by tests and the CLI. There is no
  external keychain integration beyond the optional `keyring` provider, and no
  guarantee about a GUI keychain's lock state.

## Tests

```bash
PYTHONPATH=src python -m pytest -q tests/test_secrets.py \
  tests/test_secrets_cli.py tests/test_security.py tests/test_config.py \
  tests/test_runtime.py tests/test_daemon.py tests/test_daemon_jobs.py \
  tests/test_mcp_server.py tests/test_opencode_plugin.py
.venv/bin/ruff check src tests
```

The tests use temporary directories and injected fakes for keyring, `pass`,
`op` and the runtime process factory. Job tests print a fake value from a real
child and assert that the raw log file, reads and responses do not contain it.
No test uses a real secret store, the network, `pass`, `op` or the keyring
package.
