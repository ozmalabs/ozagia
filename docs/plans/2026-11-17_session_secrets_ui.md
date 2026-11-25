# Session secret entry UI

Status: design proposal (2026-11-17; verified 2026-11-24; request slice
2026-11-25; prompt-admission slice 2026-11-25; native request UI slice
2026-11-26, offline-tested). The reference broker,
providers, redactor and in-memory session store are implemented in
`src/ozagia/core/secrets.py` with `tests/test_secrets.py`
and [`docs/secrets.md`](../secrets.md); runtime services, jobs and the daemon
resolve refs only at their execution boundaries. The host-agnostic
model-initiated **request** slice is implemented: a bounded metadata-only
pending store, daemon `secrets` actions (`request`, `request_list`,
`request_status`, `request_fulfill`, `request_decline`), the
`ozagia-secrets request/requests/fulfill/decline` CLI with a `getpass`/stdin
value path, and the OpenCode `ozagia_secret_request` tool returning a handle
and local instructions. The value never enters tool output; a fulfilled
request returns only its reference and fingerprint. MCP is deliberately
unchanged because an MCP server cannot establish harness session identity
without trusting model-supplied arguments.

The **prompt-admission** slice is also implemented in the OpenCode plugin: at
setup it registers the host's `prompt` hook, sends the prompt text to the
daemon's `capture_text` action over the local socket, replaces captured values
with session references, and remaps `prompt.files`/`agents`/`skills` mentions
(shift after a replacement, remove an overlap). It fails open and never logs
the text. This is verified against the plugin host fixture and the daemon
protocol, **not** a live OpenCode session.

The **native request UI** slice is implemented in the OpenCode add-in:
`secretsRequests`, `secretsFulfill` and `secretsDecline` RPC methods, a
`secrets` composer pill that polls pending metadata, `Provide secret…` and
`Decline secret…` commands, the host's
`ctx.dialogs.prompt({ secret: true })` dialog, and stdin-only value transport
through the runner. It is offline-tested (runner transport/closure, spec
bounds, RPC shape and source checks, metadata-only envelope); live OpenCode
Desktop verification of the native dialog and host RPC-body logging remains
open, and the add-in does not erase a host copy.

Still not implemented: live-host verification of the native secret dialog and
host logging/storage order, the HTTP provider-request fallback,
keychain/`pass`/`op`/`.env` persistence UI, revoke UI beyond the existing
`forget`, and live-host verification of prompt-storage order and RPC logging.

## Problem

Users routinely paste API keys, tokens and passwords into the chat because the
model asks for them. That puts the secret in the model context, provider
request, harness storage and often a log. The current scan is a commit gate; it
does not help a live session.

The desired flow is:

1. the model or user asks for a named secret;
2. a trusted, native UI collects it;
3. the value goes to a local store, never to the model;
4. the model receives only `secret://session/<opaque>` (or a provider ref);
5. the daemon resolves the ref only at the process boundary;
6. logs, responses and job output are redacted.

## Research summary

| Source | Relevant finding | Design consequence |
| --- | --- | --- |
| OpenCode GUI API (`@opencode/plugin/gui`) | `ctx.dialogs.prompt({secret: true})` hides the field; the host neither stores nor logs the value. `ctx.ui.TextInput` also supports `type: "password"`. `ComposerPill` can show per-session pending work. `ctx.layout.composer.insert()` can insert text into the draft. | Use the host's secret prompt for the value. Do not hold it in plugin reactive state or a custom HTML form. Use a composer pill for a pending request and insert the resulting ref into the draft. |
| OpenCode session hooks | `session.prompt` runs before model dispatch and can rewrite prompt text. `http.request` can modify a provider request. | Capture/rewrite pasted secrets at the earliest hook; keep the HTTP hook as a fallback. |
| MCP elicitation (2025-11-25 draft) | Form mode **MUST NOT** request passwords, API keys, tokens or payment credentials; use URL mode/out-of-band interaction for sensitive data. Clients must show the requesting server and allow decline/cancel. | A model request must never return the value in tool output. For third-party MCP clients, use URL mode or a client-native secure dialog; never a form schema containing a secret. |
| VS Code `SecretStorage` / `showInputBox({password: true})` | Native password input plus OS keychain storage; extensions should not log the value. | Offer OS keychain as a persistence target. No value in extension logs or settings JSON. |
| Claude Code auth | `/login` prefers account/OAuth; an env API key is explicitly approved. | Offer "Connect" / OAuth or an existing secret ref before asking for a pasted key. |
| Cursor/Cline API-key settings | Keys are entered in a dedicated settings field, not the chat. | A first-class "Add secret" surface is expected; do not rely on chat paste. |
| 1Password `op://` secret references, `op read` / `op run` | Applications receive refs; the CLI resolves at runtime. | Support `secret://op/...`, `secret://keyring/...` and `secret://pass/...` as first-class alternatives to session secrets. |
| Agent HITL patterns (CopilotKit, Haystack, Auth0 CIBA) | A tool call pauses, the client renders a trusted widget, and only the result is returned to the agent. | Model-initiated `secret_request` should pause or return a pending handle; the UI is the only channel for the value. |

## Goals

- Model-initiated secret request with explicit purpose and target.
- User-initiated "Add secret" without a model asking.
- Detection/rewrite when a secret is pasted into chat.
- Session-only storage by default; optional keychain, `.env`, `pass` or
  `secret://op/...` persistence.
- One ref returned to the model; no value in tool results, chat, logs, files
  read back, or network requests.
- Clear decline/cancel/expiry and a way to revoke.
- Works in OpenCode Desktop first, with a documented path for other MCP clients.

## Non-goals

- No long-term plaintext secret store in Ozagia state.
- No secret synchronization between machines.
- No automatic installation or authentication with third-party stores.
- No value reveal after submission (rotation/re-entry instead).
- No claim that a host's own local chat database is erased if it stored the
  prompt before our hook ran.

## Flows

### A. Model-initiated request (primary)

*Implemented in the host-agnostic slice (2026-11-25) and the OpenCode native
request UI slice (2026-11-26, offline-tested): the tool creates a
metadata-only pending request and returns a handle, while the add-in's
`secrets` composer pill and `Provide secret…` command collect the value in the
host's native dialog and send it over stdin. Live Desktop verification remains
open.*

The model calls a tool:

```json
{
  "name": "secret_request",
  "arguments": {
    "name": "OPENAI_API_KEY",
    "label": "OpenAI API key",
    "reason": "The task needs to call the OpenAI API.",
    "use": "env:OPENAI_API_KEY for the api process",
    "target": "session",
    "expires_in_s": 3600
  }
}
```

The server plugin records a pending request in the daemon (metadata only) and
returns a `request_id` or waits for the outcome. The GUI plugin detects the
pending request through a local status poll/event and shows a composer pill:

```text
[ Secret requested: OpenAI API key ]  Provide…  Use existing…  Deny
```

Clicking **Provide…** opens the native dialog. On success the waiting tool
returns:

```json
{
  "status": "provided",
  "ref": "secret://session/7f3a9c...",
  "fingerprint": "sha256:1a2b3c4d5e6f7788",
  "expires_at": "2026-11-17T14:05:00Z"
}
```

The model never sees the value. If the user denies or the request expires, the
tool returns `denied`/`expired` and the model must ask again through a new
request.

### B. User-initiated "Add secret"

A **Secrets** panel (or a button in the existing Tools panel) offers **Add
secret**. The user selects a name, enters the value in the native secure
prompt, chooses a destination and receives a ref. The ref is copied to the
composer or shown with a **Copy reference** button (the reference is safe to
share with the model).

### C. Secret pasted into chat

*Prompt-admission slice implemented (2026-11-25). The HTTP fallback and the
native toast/pill remain future work.*

The plugin registers the host's `prompt` session hook at setup:

1. it requires a string prompt text and a session id;
2. it sends `{op: "secrets", action: "capture_text", text, harness, session}`
   to the local daemon with a short (2 s) timeout;
3. the daemon detects common token/assignment shapes, captures each value in
   the session store, and returns the text with each captured value replaced
   by `secret://session/...` (detections it cannot capture become
   `[REDACTED]`);
4. the hook replaces `event.prompt.text` with that text only when the reply
   contains at least one captured `secrets` entry, so the model receives
   refs, not values;
5. it adjusts every mention in `prompt.files`, `prompt.agents` and
   `prompt.skills` from the reply's `start`/`end` spans: a mention after a
   replacement shifts by the length delta, and a mention overlapping a
   replaced span is removed. Other prompt fields, `metadata`, `delivery` and
   the ids are preserved.

If the daemon is absent, times out, returns an error or returns no captured
secret, the hook leaves the prompt exactly as it was and does not throw. The
text is never logged. This is protocol/host-fixture verification, not a live
OpenCode session; prompt-storage order and RPC logging are still open
questions.

The HTTP provider-request check is still future work. If it is added, it must
repeat the check on the outbound body as a fallback for clients or paths where
the prompt hook did not run, and must never log the body.

### D. Secret needed by a running job

The job spec carries a ref:

```toml
[runtime.services.api]
env = { OPENAI_API_KEY = "secret://session/7f3a9c..." }
```

The daemon resolves it at spawn. If the ref is missing/expired, the job is
refused with a structured `secret_expired` error. The UI shows a "Secret
expired: provide again" pill; it never retries with a value from a log or a
previous process.

### E. Connect / OAuth instead of paste

When the target provider supports OAuth or a secret manager, the dialog offers
**Connect** first:

- show the target domain and scope;
- open the system browser (not an embedded webview that the app can inspect);
- receive the callback in the local daemon;
- store the token in the chosen provider, return `secret://op/...` or
  `secret://keyring/...`;
- the model sees only the ref and the result status.

For third-party MCP clients, this is MCP URL-mode elicitation; the value is
never part of an MCP form.

## UI design

### Composer pill

- Label: `Secret requested: <label>`.
- Actions: **Provide…**, **Use existing…**, **Deny**.
- Tone: warning; danger only after expiry/denial.
- One pill per pending request; multiple requests are grouped in a menu.
- Never shows the secret, a prefix, or a suffix.

### Native secret dialog

Use `ctx.dialogs.prompt({secret: true})` for the value. Do not build a custom
password field: the host explicitly states that it neither stores nor logs the
value. A custom `TextInput type="password"` holds the value in reactive plugin
state and is a fallback only if the host prompt is unavailable.

Suggested two-step layout:

```text
┌ Ozagia needs a secret ────────────────────────────────┐
│ OpenAI API key                                        │
│ Requested by: build agent · session 4f2a              │
│ Why: the task calls the OpenAI API.                   │
│ Use: OPENAI_API_KEY for the api process               │
│                                                       │
│ Source:  (•) Paste   ( ) Existing ref   ( ) Connect   │
│ Destination: (•) This session only (1h)               │
│              ( ) OS keychain                          │
│              ( ) .env file (preview first)            │
│              ( ) 1Password / pass (preview first)     │
│                                                       │
│ The value is never sent to the model or written to    │
│ this chat. Only the reference is returned.            │
│                                                       │
│                         [ Cancel ]  [ Continue ]      │
└───────────────────────────────────────────────────────┘
```

The first dialog chooses source/destination without collecting the value.
**Continue** opens the host secret prompt with the title and description. After
submission:

```text
Stored. The model will receive:
secret://session/7f3a9c…
fingerprint: sha256:1a2b3c4d5e6f7788
expires: 1 hour

[ Copy reference ]  [ Done ]
```

The value is never shown again. The reference is safe to copy; it is scoped
and revocable.

### Secrets panel

- Pending requests with purpose and source.
- Active refs: label, ref, fingerprint, destination, created/expires, last
  used, **Forget**.
- Expired/denied requests.
- **Add secret**.
- Provider status: session store, keychain, `pass`, 1Password, `.env` target.
- No "reveal" action.

### Messages and states

| State | UI |
| --- | --- |
| Pending | Composer pill + panel row |
| Capturing | Dialog busy; no value in the transcript |
| Stored | Toast + ref + fingerprint + expiry |
| Declined | Toast; tool returns `denied` |
| Cancelled | Toast; tool returns `cancelled` |
| Expired | Warning; "Provide again" creates a new request |
| Provider unavailable | Dialog explains; offers session-only or existing ref |
| Store failure | Redacted error; no value in the message |
| Cross-session ref | Denied; "this reference belongs to another session" |

## Security requirements

1. **Trusted input only.** The value is collected by the host's native secret
   prompt, not by a custom HTML form and not by chat. The plugin must not hold
   the value in a reactive signal or storage.
2. **Local-only transport.** GUI → local daemon over a Unix socket or a
   loopback endpoint with a one-time capability token. No external network.
   The RPC body must not be logged; verify the host's RPC logging before
   relying on it. If the host logs RPC bodies, use a direct local socket or an
   encrypted one-time envelope.
3. **One-way to the model.** Tool results contain refs/fingerprints/status
   only. Never include the value in an error, log, event, audit record or
   telemetry field.
4. **Session scope.** `secret://session/<opaque>` is valid only for the same
   `(harness, session)`, in memory, with a TTL and entry/size caps. Clear on
   session end, explicit forget, eviction and process exit.
5. **Redaction everywhere.** Register captured values with the daemon redactor;
   apply it to prompt text, provider requests, tool output, job logs (write
   side, not only read side), runtime output, daemon responses and errors.
   Include exact, JSON-escaped, URL-encoded, shell-quoted and base64 forms.
6. **Least privilege.** Prefer OAuth/scoped tokens or existing secret refs over
   pasted long-lived keys. Show the destination and scope. Do not ask for a
   password for an arbitrary URL.
7. **Consent and control.** The user can decline, cancel, forget, and set a
   shorter TTL. A model cannot silently retry a denied request forever; apply
   rate limits (for example one request per 30 s and three pending per
   session).
8. **Persistence is explicit.** Writing `.env` or a secret store is a
   preview-first action with a separate confirmation. `.env` writes are mode
   `0600`, atomic, never printed in the diff, and warn when the file is
   tracked by git. Never auto-commit.
9. **Host storage caveat.** If the harness persisted the raw prompt before the
   `session.prompt` hook, Ozagia cannot erase that local copy. Document this and
   provide a "clear last message" / rotation guidance. The network/model path
   remains protected by the hook and HTTP fallback.
10. **MCP boundary.** Never use MCP form-mode elicitation for secrets. Use URL
    mode or a client-native secure dialog; show the requesting server and full
    URL/domain; require consent; never pre-fetch the URL.

## Daemon/API contract (implementation sketch)

*Implemented request slice (2026-11-25):* the daemon `secrets` actions are
`request`, `request_list`, `request_status`, `request_fulfill` and
`request_decline`; the CLI commands are `request`, `requests`, `fulfill` and
`decline`. The sketch below keeps the original names for context.

- `SessionSecretStore.capture(value, harness, session, label, ttl) -> CapturedSecret`
- `SessionSecretStore.resolve(ref, harness, session) -> SecretValue`
- `SessionSecretStore.list/forget/clear/prune`
- `secrets_capture_text(text, harness, session) -> {text, secrets[], withheld}`
- `secrets_request(name, label, reason, use, target, expires_in) -> request_id`
- `secrets_request_status(request_id) -> pending|provided|denied|expired`
- `secrets_capture_request(request_id, secret) -> ref` (local only)
- `secret://session/<token>` provider in `SecretBroker`
- `SecretRequest` audit: id, label, target, status, timestamps, fingerprint;
  no value.

## OpenCode mapping

- Server plugin: register `secret_request` tool; register RPC methods for
  status/capture; write pending metadata to the daemon; never accept a value
  from the model.
- GUI plugin: `ComposerPill` for pending requests; `ctx.dialogs.prompt({
  secret: true })` for the value; `ctx.dialogs.select` for source/destination;
  `ctx.layout.composer.insert(ref)` after user-initiated capture; `ctx.toast`
  for outcomes; `ctx.dialogs.confirm` for `.env`/store writes.
- Session hooks: `prompt` for pasted values; `http.request` fallback.
- Secrets panel: list/forget/pending; no reveal.
- If the GUI is unavailable, the tool returns `pending` with instructions and
  the CLI can provide a ref; it never sends the value.

## Acceptance tests

- A fake provider request body contains no secret after a user pastes one;
  only `secret://session/...`.
- The OpenCode prompt hook rewrites a pasted canary to a session ref, shifts
  mentions after it and removes an overlapping mention; the model-facing event
  contains no raw value. No daemon, a timeout or a bad reply leaves the prompt
  unchanged, and capture registration failure does not break setup.
- A model tool call returns only ref/fingerprint; grep the full tool result,
  logs, daemon response and audit for the secret value.
- The native secret prompt path is used; no custom password input in the
  primary flow. Offline source/structure checks confirm
  `ctx.dialogs.prompt({ secret: true })`, the pill and both commands; live
  Desktop dialog verification remains.
- Session refs expire, are cleared on session end, and are denied across
  sessions.
- Job env refs resolve at spawn; raw job log file and `job_log` output contain
  no value; child output is redacted before the log write.
- Deny/cancel/expiry paths return structured status and do not retry with a
  value.
- `.env` persistence previews with `[redacted]`, writes mode `0600`, refuses
  tracked files without an explicit override.
- MCP form-mode schema containing a password/API-key field is rejected; URL
  mode shows the full URL/domain and requires consent.
- No network calls in tests except an injected fake for OAuth/provider checks.

## Open questions

1. Does OpenCode's `session.prompt` hook run before the prompt is persisted to
   its local session database? If not, can the plugin rewrite or delete that
   record?
2. Does the OpenCode plugin RPC layer log request bodies at debug level? If so,
   the GUI-to-server capture path needs a direct Unix socket or an encrypted
   envelope.
3. Should the model be allowed to request secrets in plan mode or only in
   build/execute modes? Recommended: only when a tool actually needs the ref,
   with a visible purpose and a per-agent policy.
4. Which persistence targets ship first: session-only + keyring + `.env` +
   `op`, or session-only first?
5. Should a captured session secret be usable by subagents? Recommended: yes
   for the same root session, with explicit scoping; default no for unrelated
   sessions.
6. How long should a pending request live? Recommended 2 minutes for the UI,
   configurable up to 10 minutes; the tool should not block indefinitely.

## Rollout

1. **User-initiated + paste detection**: the in-memory session store, OpenCode
   prompt hook and daemon-side redaction are implemented; the prompt hook is
   verified against the plugin host fixture, not a live session. The native
   secret prompt and composer pill are implemented in the add-in and
   offline-tested. The Secrets panel remains.
2. **Model-initiated request**: `secret_request` tool, pending pill, waiting
   status and result ref are implemented. The GUI uses the host's native
   secret dialog and the CLI/stdin path; live Desktop verification remains.
3. **Persistence**: keyring, `.env`, `pass`, 1Password previews/applies; expiry
   and forget.
4. **Connect/OAuth and MCP URL mode**; provider-specific validation.
5. **Host verification**: test the native secret dialog, prompt-storage order,
   host RPC-body logging and session storage; document the exact guarantee.
   This slice does not claim the host's own storage is erased.
