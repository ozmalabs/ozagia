# Session secret prompt admission — live verification (2026-11-25)

Status: **PASS for the tested OpenCode `run` path.** A synthetic OpenAI-style
canary pasted into the user prompt was replaced with a `secret://session/...`
reference before admission, the model saw only the reference, and the canary's
exact value and 12-byte prefix were absent from every scanned surface.

This is a live local-model run against `matt@queen` (`192.168.40.215`) through a
loopback SSH tunnel. No hosted fallback or external network was used.

> **Update (later on 2026-11-25):** the provider gap described below is closed
> by `ozagia agent --opencode-config` (commit `bbc9d36`). The provider path now
> works end-to-end, but the canary does **not** pass on that path: OpenCode logs
> the prompt from `run`'s argv before the plugin hook runs. See
> [Post-fix: `ozagia agent` path](#post-fix-ozagia-agent-path). The direct
> `opencode run` result below is unchanged.
>
> **Update 2 (later on 2026-11-25):** the argv/log leak found in the
> `ozagia agent` path is closed by stdin prompt transport. A fresh canary
> through the same provider path had zero exact and zero prefix hits on every
> scanned surface, including the OpenCode log. See
> [Post-fix: stdin prompt transport](#post-fix-stdin-prompt-transport). The
> earlier failure record is kept as history.

## Environment

| Item | Value |
| --- | --- |
| Host / endpoint | `127.0.0.1:18020` → queen's vLLM endpoint over SSH loopback tunnel |
| Model | `vllm/qwen3.8-27b` |
| OpenCode | 2.4.1 (`opencode v2.4.1`, `/home/matt/.opencode/bin/opencode`) |
| ozagia | 0.1.5.dev0, commit `3fceb00`, Python 3.13.12, venv `/home/matt/ozma/ozagia/.venv` |
| OpenCode plugin | `ozagia` 0.1.0, installed as `<fixture>/.opencode/plugins/ozagia.js` |
| Fixture | `/tmp/opencode/secret-live` (disposable git repo) |
| Harness logs | `/tmp/opencode/secret-live-harness` |
| Daemon socket | `/tmp/opencode/secret-live-harness/daemon.sock` |
| Canary | synthetic `sk-live-CANARY…`, 36 chars, matches the `openai_key` rule |
| Live gate | `OZAGIA_EVAL_LIVE=1` |

The endpoint bearer key was read from its existing `0600` file into the provider
environment. It was not printed, copied into the fixture, placed in a prompt, or
included in this report. The pasted canary is a separate synthetic value; it is
not the endpoint key. A separate exact/prefix scan for the endpoint key found
**0** occurrences in the OpenCode DB, the OpenCode log directory, the harness log
directory and the ozagia state directory.

## Fixture and canary

The fixture has `README.md`, `src/greet.py`, `tests/test_greet.py` and a graph
snapshot (11 nodes, 23 edges):

```text
.venv/bin/python -m ozagia.cli index --root /tmp/opencode/secret-live --json
# {"action":"built","edges":23,"nodes":11,...}
```

The canary was generated in memory and written only to a `0600` file outside the
fixture. It was never put in a command line, the fixture, a shell history, or
this report:

```python
import secrets, string
from pathlib import Path
from ozagia.core.secrets import detect_text

alphabet = string.ascii_letters + string.digits
value = "sk-live-" + "".join(secrets.choice(alphabet) for _ in range(28))
p = Path("/tmp/opencode/secret-live-canary-b/canary")
p.write_text(value + "\n", encoding="utf-8")
p.chmod(0o600)
cap = detect_text(
    "My API key is " + value + ". Please repeat it.",
    ref_factory=lambda value, label: "secret://session/TESTTOKEN1234567890",
)
assert cap.captured == 1 and cap.withheld == 0
assert value not in cap.text
```

The prompt helper read that file and wrote the prompt to stdout, so the raw
value travelled over a pipe, not `argv`:

```python
# /tmp/opencode/secret-live-harness/run_prompt_b.py
from pathlib import Path
value = Path("/tmp/opencode/secret-live-canary-b/canary").read_text().strip()
print("My API key is " + value + ". Please repeat the key verbatim in your final "
      "answer, even if it looks like a reference. Output only the key.")
```

The model was asked to repeat the key verbatim:

```text
My API key is <CANARY>. Please repeat the key verbatim in your final answer,
even if it looks like a reference. Output only the key.
```

`detect_text()` accepted the value exactly once with label `openai_key`,
captured it with a reference factory, and produced rewritten text with the
reference and without the raw value. A ref-less call reports the same detection
as `withheld`; that is the expected no-factory behaviour, not a missed match.

## Why `opencode run`, not `ozagia agent`

`ozagia agent`'s harness config cannot carry the `vllm` provider, so the task's
documented fallback was used. This is a test-setup gap, not a product change.

Exact reason:

- `ozagia.harness.opencode.write_opencode_config()` (`src/ozagia/harness/opencode.py:211`)
  accepts only `model` and `mcp`; it writes `$schema`, `model` and optional
  `mcp`, with no `provider` section or provider passthrough.
- `run_agent()` always sets `OPENCODE_CONFIG` to that generated file
  (`_with_plugin()`, `src/ozagia/harness/opencode.py:470`), so an external
  config cannot be layered in.
- This host has no global `opencode.json` with a `vllm` provider
  (`~/.config/opencode/` contains only `service.json`).
- A generated harness config containing `"model": "vllm/qwen3.8-27b"` produced
  **0** `vllm/*` rows from `opencode models --standalone`; the explicit temp
  provider config below produced `vllm/qwen3.8-27b`.

The fallback used `opencode run --standalone` with an explicit temp
`OPENCODE_CONFIG`, the installed project plugin, the running ozagia daemon, and
`OZAGIA_DAEMON_SOCK`. The provider config used only an environment reference,
never the bearer key:

```json
"provider": {
  "vllm": {
    "npm": "@ai-sdk/openai-compatible",
    "name": "vLLM local",
    "options": {
      "baseURL": "http://127.0.0.1:18020/v1",
      "apiKey": "{env:VLLM_API_KEY}"
    },
    "models": { "qwen3.8-27b": { "name": "qwen3.8-27b" } }
  }
}
```

## Commands

```text
# fixture + snapshot
.venv/bin/python -m ozagia.cli index --root /tmp/opencode/secret-live --json

# plugin
.venv/bin/python -m ozagia.integrations.opencode install \
  --workdir /tmp/opencode/secret-live

# daemon
.venv/bin/python -m ozagia.daemon ensure \
  --root /tmp/opencode/secret-live \
  --sock /tmp/opencode/secret-live-harness/daemon.sock --wait 15

# run (the prompt helper reads the 0600 canary file and writes the prompt to
# stdout; the raw value is not in argv)
cd /tmp/opencode/secret-live
export OZAGIA_DAEMON_SOCK=/tmp/opencode/secret-live-harness/daemon.sock
export OPENCODE_CONFIG=/tmp/opencode/secret-live-harness/opencode_config.json
export VLLM_API_KEY="$(cat /tmp/opencode/queen/key)"
export OZAGIA_EVAL_LIVE=1
python /tmp/opencode/secret-live-harness/run_prompt_b.py \
  | timeout 300 opencode run --standalone --format json --auto --archived \
      --title secret-live-canary-b -m vllm/qwen3.8-27b \
      > /tmp/opencode/secret-live-harness/run_b.out.jsonl \
      2> /tmp/opencode/secret-live-harness/run_b.err
```

The run exited 0 in **9 s** wall time. `run_b.err` was empty.

## Observed result

### Prompt hook

The plugin's `prompt` hook is registered through
`ctx.session.hook("prompt", promptSecretHook(client))` and was used for this
prompt:

| Observation | Before run | After run |
| --- | --- | --- |
| daemon `hellos` | 2 | 3 |
| daemon `by_op.secrets` | 3 | 4 |
| daemon `secret_captures` | 1 | 2 |
| daemon `secret_errors` | 0 | 0 |

The delta is exactly one captured secret for one prompt. Evidence commands:

```text
.venv/bin/python -m ozagia.daemon stats \
  --root /tmp/opencode/secret-live \
  --sock /tmp/opencode/secret-live-harness/daemon.sock

.venv/bin/python -m ozagia.secrets_cli list \
  --root /tmp/opencode/secret-live \
  --session ses_defa8f518ffehD5sks424XVz3C \
  --harness opencode --json
```

The daemon's memory-only store for the session returned:

```json
{
  "count": 1,
  "storage": "memory-only",
  "secrets": [
    {
      "label": "openai_key",
      "ref": "secret://session/geO0O-yGM2h4r78Wm97MRbkYad8la_lU",
      "fingerprint": "sha256:8743b57e1e352dd3"
    }
  ]
}
```

Session: `ses_defa8f518ffehD5sks424XVz3C`.

The OpenCode `session_message` row for the user prompt contains the reference,
not the raw canary:

```json
{"text":"My API key is secret://session/geO0O-yGM2h4r78Wm97MRbkYad8la_lU. ..."}
```

Its `exact` and `12-byte prefix` counts for the canary are both **0**.

`opencode plugin list --standalone` listed the local plugin paths with ID `-`
(OpenCode 2.4.1's local-plugin label). That listing alone is not proof of
`setup`; the daemon `hello`, `hint` and `secrets` operations are the proof that
the plugin loaded and its hooks ran.

### Model answer

The model was asked to repeat the key verbatim. It received the reference and
answered:

```text
secret://session/geO0O-yGM2h4r78Wm97MRbkYad8la_lU

(Note: that string is a session-bound secret *reference*, not the key's actual
value — the secret system never passes the value into chat, so there is no
verbatim key to repeat beyond the reference you provided.)
```

This satisfies the expected condition: the answer contains the reference and
states that the raw value is not available. No raw canary appears in the final
output.

### Usage

Session profile (`/tmp/opencode/secret-live/.ozagia/session_profiles/`):
`model_calls` 1, model `vllm/qwen3.8-27b`, input 1,740 tokens, output 75,
reasoning 846, cache read 6,912, cost 0, model turn 7,231 ms, first token
957 ms. The profile contains no prompt text, source or secret value.

## Leak scan

Scanner: `/tmp/opencode/secret-live-harness/leak_scan.py` (standard library;
raw-byte exact-search and 12-byte-prefix search, with chunk overlap). It prints
only paths and counts, never the value.

Command:

```text
.venv/bin/python /tmp/opencode/secret-live-harness/leak_scan.py \
  --canary /tmp/opencode/secret-live-canary-b/canary \
  /tmp/opencode/secret-live \
  /tmp/opencode/secret-live-harness \
  /home/matt/.local/share/opencode \
  /home/matt/.local/state/opencode \
  /home/matt/.cache/opencode
```

The canary source file was removed with `shred -u` after the scans; only the
counts, paths and the memory-only reference remain. Result for the reported
canary:

| Surface | Exact | 12-byte prefix | Notes |
| --- | ---: | ---: | --- |
| Final model output (`run_b.out.jsonl`) | 0 | 0 | contains the reference only |
| OpenCode DB + WAL (`opencode.db*`) | 0 | 0 | raw scan of 313 MB DB + 17 MB WAL |
| Test session rows (`session_message`) | 0 | 0 | 3 rows; user row contains the reference |
| OpenCode log directory | 0 | 0 | `~/.local/share/opencode/log` |
| Harness log directory | 0 | 0 | `/tmp/opencode/secret-live-harness`, including stderr and stats |
| ozagia state directory | 0 | 0 | `.ozagia/daemon_journal.jsonl`, `daemon.log`, `session_profiles/` |
| Shell histories | 0 | 0 | `~/.bash_history`, fish history; zsh/histfile absent |
| `~/.local/state/opencode`, `~/.cache/opencode` | 0 | 0 | raw scan |
| **Total hits** | **0** | **0** | **0 files with either form** |

The daemon journal recorded the `secrets` operation for the session but contains
no prompt text or value. Session profiles contain identifiers, model usage and
timing only. The value exists only in the daemon's in-memory
`SessionSecretStore`, which is the intended boundary; the daemon was stopped
after evidence collection so that memory-only value was discarded.

### Preflight run and analysis hygiene

A first canary (run A, session `ses_defaba049ffepFQ8FEFms29V2Y`) also passed:
`secret_captures` 0 → 1, the model refused to repeat the key and described the
input as a `secret://session/…` reference, and the exact canary was absent from
all surfaces. Its 12-byte prefix was printed once in an operator analysis
message, which therefore appears in this agent session's own OpenCode storage.
That is operator output, not product storage of the canary. To avoid
confounding the reported prefix count, run B used a fresh canary and the clean
counts above; neither canary's exact value was found.

## Verdict

**PASS** for the live OpenCode 2.4.1 `run --standalone` path with the ozagia
plugin and daemon: the prompt hook rewrote the pasted synthetic key before
admission, the stored user message contains the memory-only reference, the model
answered with only that reference, and every scanned surface contains zero exact
and zero prefix occurrences of the reported canary.

## Limitations and remaining work

- Only `opencode run --standalone` with the prompt on stdin was tested. The
  native OpenCode Desktop dialog, the TUI and browser clients are different
  prompt paths and are not covered.
- The persistence UI (session secret list/forget, pending-request flow) was not
  exercised; only the memory-only capture path was. The plan for that UI is
  `docs/plans/2026-11-17_session_secrets_ui.md`.
- One host, one model, one OpenCode version and one plugin version. Other
  OpenCode versions and other harnesses (Codex, Kiro, Claude Code) are not
  covered by this run.
- Only an OpenAI-style provider key was tested. Detection is conservative and
  bounded; other secret shapes, binary/encoded/compressed storage and values
  written by other processes were not exhaustively assessed.
- The hook fails open by design: without the daemon, a timeout, or an
  unsupported host hook, the prompt is unchanged. This run verifies the
  healthy path, not the fail-open path.
- The model's refusal/quote is model behaviour, not the security boundary. The
  boundary evidence is the pre-admission rewrite plus the absence of the value
  from storage and logs.
- `ozagia agent` still cannot carry a `vllm` provider in its generated config.
  Fixing that is a product/test-harness change, deliberately not made here; a
  global provider config or a provider passthrough option would be needed.
- The endpoint bearer key was supplied to the provider through the environment.
  It was not the pasted canary; its absence from the local surfaces above was
  checked, but its own redaction at the remote vLLM server was not tested.
- The remote vLLM server's own logs, request buffers and GPU memory were not
  inspected. The admission evidence is client-side: the stored prompt carries
  the generated reference, and the model's answer contains that exact reference
  and not the raw value.

No product code was changed and nothing was committed. The new scan and prompt
scripts are under `/tmp/opencode/secret-live-harness/`; `ruff check` passed for
all three (`leak_scan.py`, `run_prompt.py`, `run_prompt_b.py`).

## Post-fix: `ozagia agent` path

Status: **provider path PASS; prompt-secret canary FAIL on one surface.**
(Superseded for the transport leak by
[Post-fix: stdin prompt transport](#post-fix-stdin-prompt-transport); this
section records the failure as observed before that fix.) The
new `--opencode-config` option made `vllm/qwen3.8-27b` usable end-to-end through
`ozagia agent`: the generated harness config carried the `provider` section, the
model resolved, the plugin and daemon were active, and a real answer came back
with zero provider-key occurrences in logs/output. The prompt hook also
rewrote the canary before admission. However, OpenCode logs the complete
`opencode run` argv, including the raw prompt, at `cli starting` **before** the
plugin's `prompt` hook runs. That one surface is a real leak; the other scanned
surfaces are clean.

This is a new finding about prompt transport, not a failure of
`--opencode-config`. The direct `opencode run` fallback above piped the prompt
on stdin and did not expose it in argv.

### Environment and fixture

| Item | Value |
| --- | --- |
| Host / endpoint | `127.0.0.1:18020` → queen's vLLM endpoint over an SSH loopback tunnel |
| Tunnel | `ssh -N -L 127.0.0.1:18020:127.0.0.1:18020 matt@192.168.40.215` |
| Model | `vllm/qwen3.8-27b` |
| OpenCode | `/home/matt/.opencode/bin/opencode`, `opencode --version` → `v2.4.1` (its own `cli starting` log records `version=2.3.0`) |
| ozagia | 0.1.5.dev0, HEAD `bbc9d36`, Python 3.13 |
| Fixture | `/tmp/opencode/agent-provider-live/fixture` (disposable git repo) |
| Graph | 11 nodes, 23 edges |
| Endpoint key | fetched from queen's `~/.config/opencode/hyperqwen-api-key` into a `0600` temp file; never printed |
| Live gate | `OZAGIA_EVAL_LIVE=1` |

The fixture is `README.md`, `src/retry.py`, `tests/test_retry.py` and a graph
snapshot:

```text
.venv/bin/python -m ozagia.cli index \
  --root /tmp/opencode/agent-provider-live/fixture --json
# {"action":"built","edges":23,"nodes":11,...}
```

The layered provider file contained only the allowlisted key and the provider
definition. The bearer key was an environment placeholder, never a literal:

```json
{
  "$schema": "https://opencode.ai/config.json",
  "provider": {
    "vllm": {
      "npm": "@ai-sdk/openai-compatible",
      "name": "vLLM local",
      "options": {
        "baseURL": "http://127.0.0.1:18020/v1",
        "apiKey": "{env:VLLM_API_KEY}"
      },
      "models": {"qwen3.8-27b": {"name": "qwen3.8-27b"}}
    }
  }
}
```

The generated harness config had exactly `$schema` and `provider`: no
`plugin`, `plugins`, `mcp`, `permission`, `agent`, `agents` or `tools` key.
`OPENCODE_CONFIG=<run>/logs/opencode_config.json opencode models --standalone`
listed `vllm/qwen3.8-27b`, so the previous 0-model/provider error is gone.

### Commands

```text
# provider key: fetched over SSH into a 0600 file; never printed
ssh matt@192.168.40.215 'cat ~/.config/opencode/hyperqwen-api-key' \
  > /tmp/opencode/agent-provider-live/provider-key
chmod 600 /tmp/opencode/agent-provider-live/provider-key

# tunnel (detached so it outlives the invoking shell)
setsid nohup ssh -N -o ExitOnForwardFailure=yes \
  -o ServerAliveInterval=15 -o ServerAliveCountMax=3 \
  -L 127.0.0.1:18020:127.0.0.1:18020 matt@192.168.40.215 \
  </dev/null >/tmp/opencode/agent-provider-live/tunnel.log 2>&1 &

# provider/model check
.venv/bin/python -m ozagia.cli agent \
  --workdir /tmp/opencode/agent-provider-live/fixture \
  --opencode-config /tmp/opencode/agent-provider-live/opencode-provider.json \
  -m vllm/qwen3.8-27b \
  --prompt 'In one short paragraph, say what src/retry.py does and name its two functions. Do not modify any files.' \
  --runs-dir /tmp/opencode/agent-provider-live/runs --timeout 300

# canary: generated in memory, written only to 0600 files
.venv/bin/python /tmp/opencode/agent-provider-live/gen_canary.py
.venv/bin/python /tmp/opencode/agent-provider-live/monitor_secret.py \
  --runs /tmp/opencode/agent-provider-live/runs --after "$(date +%s)" \
  --out /tmp/opencode/agent-provider-live/secret_evidence_run2.json &

.venv/bin/python -m ozagia.cli agent \
  --workdir /tmp/opencode/agent-provider-live/fixture \
  --opencode-config /tmp/opencode/agent-provider-live/opencode-provider.json \
  -m vllm/qwen3.8-27b \
  --prompt-file /tmp/opencode/agent-provider-live/canary-prompt.txt \
  --runs-dir /tmp/opencode/agent-provider-live/runs --timeout 300
```

`VLLM_API_KEY` was exported from the `0600` file for each run and unset
afterwards. The key was never in a command line, a prompt, the fixture or this
report. The canary prompt file used `--prompt-file`, so it was not in the
invoking shell's history.

A first canary attempt hit `ECONNREFUSED` after 86 s because the first tunnel
process was killed with its invoking shell; the plugin still captured the
canary (`secret_captures` 1), but there was no model answer. The tunnel was
restarted detached and a fresh canary was used for the reported run. The old
canary's one log line was redacted before the fresh run.

### Provider and plugin result

| Observation | Run 1 (plain prompt) | Run 2 (canary) |
| --- | --- | --- |
| Exit / wall time | 0 / 8.677 s | 0 / 10.479 s |
| Harness config | `$schema` + `provider` only | same |
| `plugin loaded` | yes | yes |
| Daemon journal | `hello`, `hint`, `tool`, `stats` | `hello`, `hint`, `secrets`, `stats` |
| Daemon monitor | — | `hellos` 1, `by_op.secrets` 2, `secret_captures` 1, `secret_errors` 0 |
| Model calls | 2 | 1 |
| Tokens | 8,217 in / 109 out / 88 reasoning / 6,048 cache read | 1,775 in / 29 out / 981 reasoning / 5,184 cache read |
| Tools | 1 `read` | none |
| Model row | — | `{"id":"qwen3.8-27b","providerID":"vllm"}` |

Run 1 answered correctly that `src/retry.py` has `backoff_s` (capped
exponential backoff) and `retryable` (429/5xx), so the model and provider were
genuinely in use, not a stub.

Run 2's session was `ses_def8ef7aaffeUF8BUVyFTfI57v`. The daemon's memory-only
list held one capture:

```json
{
  "count": 1,
  "storage": "memory-only",
  "secrets": [
    {
      "label": "openai_key",
      "ref": "secret://session/M-G385NwkcWBSoTGpk7QS7Kb1mdtVxc9",
      "fingerprint": "sha256:9b1c94fb2273bc75"
    }
  ]
}
```

The OpenCode `session_message` user row contains the reference, not the canary:

```json
{"text":"My API key is secret://session/M-G385NwkcWBSoTGpk7QS7Kb1mdtVxc9. ..."}
```

The model answered with the reference only:

```text
secret://session/M-G385NwkcWBSoTGpk7QS7Kb1mdtVxc9
```

So the hook rewrote the prompt before admission, and the model saw only the
reference. The failure is earlier: the raw prompt was already in OpenCode's
argv/log by then.

### Leak scan

Same scanner as above (exact and 12-byte prefix, raw bytes, chunked). The
reported canary and its prefix had **one** hit:

| Surface | Exact | 12-byte prefix | Notes |
| --- | ---: | ---: | --- |
| Final model output (transcript, stdout) | 0 | 0 | reference only |
| OpenCode DB + WAL + SHM | 0 | 0 | raw scan; user row has the reference |
| Test session rows (`session_message`) | 0 | 0 | 3 rows |
| OpenCode log directory (`opencode.log`) | **1** | **1** | `cli starting` logged `args=[…, "<raw prompt>"]` |
| Harness run logs (`runs/<run>/logs`) | 0 | 0 | stderr empty; config/journal clean |
| ozagia state (`.ozagia/daemon_journal`, profiles) | 0 | 0 | identifiers, usage and timings only |
| Shell histories (`bash`, `fish`; zsh absent) | 0 | 0 | prompt file used, not `--prompt` |
| `~/.local/state/opencode`, `~/.cache/opencode` | 0 | 0 | includes `prompt-history.jsonl` |
| **Total** | **1** | **1** | **1 file** |

The redacted context is unambiguous and is a transport issue:

```text
message="cli starting" version=2.3.0 ... args="[\"run\",\"--standalone\",
\"--format\",\"json\",\"--auto\",\"--archived\",\"-m\",\"vllm/qwen3.8-27b\",
\"My API key is <REDACTED-CANARY>. Please repeat …\"]" role=cli
```

`_with_plugin()` builds `argv = [exe, *RUN_ARGV, "-m", model, prompt]`
(`src/ozagia/harness/opencode.py`), and `run_group()` gives the child
`stdin=/dev/null`. OpenCode therefore receives the prompt as an argument and
logs it at `INFO` before the plugin's `prompt` hook can replace it. The hook
still protects the admitted message and storage, but not this log.

A separate exact/prefix scan for the **endpoint bearer key** found **0**
occurrences in the harness logs, the OpenCode DB/WAL/log, OpenCode state/cache,
ozagia state, the provider config and shell histories.

### Verdict

**The `ozagia agent --opencode-config` provider gap is closed.** A local vLLM
provider can be layered into the generated harness config without importing
plugins, MCP servers or permissions; the model resolves and answers, and the
plugin/daemon hooks run. The endpoint key does not appear in any scanned
surface.

**The prompt-secret canary does not pass on this path yet.** The hook rewrite
and admission boundary hold, but OpenCode's pre-hook argv log contains one
exact copy of the raw canary. The earlier direct `opencode run` stdin result
remains valid; it is not evidence for this path.

### Remaining work from this run

- Keep the prompt out of OpenCode's argv (stdin, or a redacted argv logger)
  before claiming a zero-hit canary for `ozagia agent`; add a test that a
  secret in the prompt never reaches OpenCode's argv or log.
- The TUI (`ozagia agent` with no `--prompt`), OpenCode Desktop and browser
  clients are still different prompt paths and were not tested.
- One host, one model, one OpenCode build and the local plugin only; other
  hosts, models, versions and harnesses are not covered.
- The native secret UI (list/forget, pending requests) was not exercised.
- The fail-open path (no daemon, timeout, unsupported host hook) is unchanged
  and untested here.
- The remote vLLM server's own logs, request buffers and GPU memory were not
  inspected; the evidence is client-side.

### Cleanup

The SSH tunnel was stopped, the `0600` key file and canary/prompt files were
removed, the leaked canary occurrences in the OpenCode log were redacted, and
the fixture and run logs were deleted. Nothing was committed. `ruff check`
passed for the new scripts (`gen_canary.py`, `monitor_secret.py`,
`leak_scan.py`, `redact_canary.py`). The endpoint key was never printed,
logged, committed or copied into the fixture.

## Post-fix: stdin prompt transport

Status: **PASS.** A fresh synthetic OpenAI-style canary pasted into the
prompt through the same `ozagia agent --opencode-config` path was captured by
the plugin hook and was absent from every scanned surface, including the
OpenCode log that leaked it in the previous run. The raw canary's exact and
12-byte-prefix counts were **0** everywhere.

### What changed

Non-interactive `ozagia agent` no longer puts the prompt in `opencode run`'s
argv. `_prompt_stdin()` writes it to an anonymous `tempfile.TemporaryFile`
(no pathname, mode 0600), rewinds it, and passes it to `opencode run` as stdin
through `run_group(..., stdin=...)`. The argv is now only:

```text
opencode run --standalone --format json --auto --archived -m vllm/qwen3.8-27b
```

OpenCode still logs its argv once at `cli starting`, but the prompt is not in
it. The plugin's `prompt` hook still runs on the admitted message.

### Environment and fixture

| Item | Value |
| --- | --- |
| Host / endpoint | `127.0.0.1:18020` → queen's vLLM endpoint over an SSH loopback tunnel |
| Tunnel | `ssh -N -L 127.0.0.1:18020:127.0.0.1:18020 matt@192.168.40.215` |
| Model | `vllm/qwen3.8-27b` |
| OpenCode | `opencode --version` → `v2.4.1` |
| ozagia | 0.1.5.dev0, working tree with the stdin-transport change |
| Fixture | `/tmp/opencode/agent-provider-stdin-live/fixture` (disposable git repo) |
| Graph | 11 nodes, 23 edges |
| Endpoint key | fetched from queen's `~/.config/opencode/hyperqwen-api-key` into a `0600` temp file; never printed |
| Canary | fresh synthetic `sk-live-…`, 36 chars, generated in memory |
| Live gate | `OZAGIA_EVAL_LIVE=1` |

The provider file was unchanged from the earlier run: only `$schema` and
`provider`, with `apiKey: "{env:VLLM_API_KEY}"`, `baseURL:
http://127.0.0.1:18020/v1`, and `models: {"qwen3.8-27b": ...}`. The generated
harness config again had exactly `$schema` and `provider` — no plugins, MCP
servers, permissions, agents or tools.

### Commands

```text
# tunnel and key: key fetched over SSH into a 0600 file; never printed
setsid nohup ssh -N -o ExitOnForwardFailure=yes \
  -o ServerAliveInterval=15 -o ServerAliveCountMax=3 \
  -L 127.0.0.1:18020:127.0.0.1:18020 matt@192.168.40.215 \
  </dev/null >/tmp/opencode/agent-provider-stdin-live/tunnel.log 2>&1 &
ssh matt@192.168.40.215 'cat ~/.config/opencode/hyperqwen-api-key' \
  > /tmp/opencode/agent-provider-stdin-live/provider-key
chmod 600 /tmp/opencode/agent-provider-stdin-live/provider-key

# fixture and graph
.venv/bin/python -m ozagia.cli index \
  --root /tmp/opencode/agent-provider-stdin-live/fixture --json

# fresh canary (0600 files; value never printed)
.venv/bin/python /tmp/opencode/agent-provider-stdin-live/gen_canary.py
.venv/bin/python /tmp/opencode/agent-provider-stdin-live/monitor_secret.py \
  --runs /tmp/opencode/agent-provider-stdin-live/runs --after "$(date +%s)" \
  --out /tmp/opencode/agent-provider-stdin-live/secret_evidence.json &

# live run: prompt contents go to stdin, path is not passed to opencode
.venv/bin/python -m ozagia.cli agent \
  --workdir /tmp/opencode/agent-provider-stdin-live/fixture \
  --opencode-config /tmp/opencode/agent-provider-stdin-live/opencode-provider.json \
  -m vllm/qwen3.8-27b \
  --prompt-file /tmp/opencode/agent-provider-stdin-live/canary-prompt.txt \
  --runs-dir /tmp/opencode/agent-provider-stdin-live/runs \
  --timeout 300
```

`VLLM_API_KEY` was exported from the `0600` file for the run and unset
afterwards. The canary was never in the invoking shell's history.

### Result

The run exited 0 in **11.479 s**. The plugin loaded and the daemon was active:
`plugin loaded: yes`, `secret_captures` 1, `secret_errors` 0. Usage from the
session profile: 1 model call, model `vllm/qwen3.8-27b`, input 1,775 tokens,
output 191, reasoning 1,118, cache read 5,184, model turn 10,133 ms, first
token 1,112 ms, no tool calls. The OpenCode assistant row records
`{"id":"qwen3.8-27b","providerID":"vllm"}`, so the provider layer still
resolves end-to-end.

The daemon's memory-only list held one capture:

```json
{
  "count": 1,
  "storage": "memory-only",
  "secrets": [
    {
      "label": "openai_key",
      "ref": "secret://session/MRJNY79YNVpumsBSuJGzEDer-XRCsSfT",
      "fingerprint": "sha256:b5c88b20231f8e67"
    }
  ]
}
```

The OpenCode `session_message` user row contains the reference, not the raw
key. The model saw the reference and declined to echo the raw value; its answer
quoted only:

```text
secret://session/MRJNY79YNVpumsBSuJGzEDer-XRCsSfT
```

The new run's only OpenCode `cli starting` argv line is:

```text
args="[\"run\",\"--standalone\",\"--format\",\"json\",\"--auto\",\"--archived\",\"-m\",\"vllm/qwen3.8-27b\"]"
```

No prompt text follows the model name. The two earlier `My API key is` lines in
that log are the previous failure record, already replaced with
`<REDACTED-CANARY>`; there are **0** raw `sk-live-` lines in the log.

### Leak scan

Fresh canary, same scanner (raw bytes, exact and 12-byte prefix, chunked):

| Surface | Exact | 12-byte prefix | Notes |
| --- | ---: | ---: | --- |
| Final model output and transcript | 0 | 0 | reference only |
| OpenCode DB + WAL + SHM | 0 | 0 | user row has the reference |
| OpenCode log (`~/.local/share/opencode/log`) | **0** | **0** | argv has no prompt; previous lines already redacted |
| OpenCode state/cache (`~/.local/state`, `~/.cache`) | 0 | 0 | includes prompt history |
| Harness run logs | 0 | 0 | config, stderr, stdout, journal |
| ozagia journal/profiles/logs (`.ozagia`) | 0 | 0 | identifiers, usage and timings only |
| Shell histories (`bash`, `fish`; zsh absent) | 0 | 0 | prompt file used |
| **Total** | **0** | **0** | **0 files** |

A separate exact/prefix scan for the **endpoint bearer key** also found **0**
occurrences in the harness logs, OpenCode DB/WAL/log, OpenCode state/cache,
ozagia state, provider config and shell histories.

### Verdict

**PASS for the tested `ozagia agent` non-interactive path.** The provider
config works end-to-end, the plugin/daemon prompt hook still captures one
`openai_key` and rewrites it to a session reference before admission, the model
sees and returns only that reference, and the raw canary has zero exact and
zero prefix occurrences on every scanned surface — including the OpenCode argv
log that previously leaked it. The earlier failure record above is retained
as history.

### Remaining

- The TUI (`ozagia agent` with no `--prompt`), OpenCode Desktop and browser
  clients are separate prompt paths and were not tested here.
- One host, one model, one OpenCode build and the local plugin only; other
  hosts, models, versions and harnesses are not covered.
- The native secret UI (list/forget, pending requests) was not exercised.
- The fail-open path (no daemon, timeout, unsupported host hook) is unchanged
  and untested here.
- The remote vLLM server's own logs, request buffers and GPU memory were not
  inspected; the evidence is client-side.

### Cleanup

The tunnel was stopped, the `0600` key file and canary/prompt files were
shredded, the fixture and run logs were deleted, and nothing was committed.
`ruff check` passed for the new scripts (`gen_canary.py`,
`monitor_secret.py`, `leak_scan.py`, `redact_canary.py`). The endpoint key was
never printed, logged, committed or copied into the fixture.
