# Executing the usage-findings features

Date: 16 Nov 2026. Source: `2026-11-16_usage_findings.md` (the 16 ranked
features). This plan turns that list into workstreams that agents can run in
parallel, in waves, with a merge gate between waves.

Status: Waves 1–4 implemented in large part, verified 2026-11-24. W1–W11 landed
in `integrations/hookcore.py`, `integrations/codex/`, `integrations/kiro/`, the
`intent/` modules (read ledger, compound shell, structured data, patch
recovery, affected tests, lint, path resolution and ownership),
`daemon/jobs.py` and their focused tests (`tests/test_codex_hooks.py`,
`tests/test_kiro_hooks.py`, `tests/test_read_ledger.py`,
`tests/test_intent_compound.py`, `tests/test_structured.py`,
`tests/test_daemon_jobs.py`, `tests/test_affected_tests.py`,
`tests/test_edit_lint.py`, `tests/test_paths_owners.py`). W12–W15 landed as
`intent/replace.py` (`tests/test_replace.py`), `core/ops_graph.py`
(`tests/test_ops_graph.py`), research user positions
(`tests/test_research_positions.py`) and fetch fallback
(`tests/test_research_fetch_fallback.py`). What remains is verification, not a
missing wave: no post-wave live comparison of tokens, tool calls or wall time
has been run, so the savings in the findings document are still unverified;
Codex and Kiro hooks are implemented but untested against a live host. The wave
specifications below are the original plan text.

## Facts the plan depends on

- **Codex 0.169 has command hooks** (docs: learn.chatgpt.com/docs/hooks).
  The wire format is Claude Code's: stdin JSON with `session_id`, `cwd`,
  `hook_event_name`, `tool_name`, `tool_input`, `tool_response`.
  - Files: `<repo>/.codex/hooks.json` or `~/.codex/hooks.json`, or inline
    `[hooks]` in `config.toml`. Project hooks load only in a trusted project,
    and every non-managed hook must be reviewed and trusted by the user in
    Codex before it runs (trust is recorded against the hook's hash).
  - `exec_command` matches as `Bash` (`tool_input.command`). `apply_patch`
    matches as `apply_patch`/`Edit`/`Write`, and `tool_input.command` is the
    patch text. `write_stdin` polls do not run PreToolUse.
  - PreToolUse can deny (`permissionDecision: "deny"` with a reason the
    model sees, or exit 2), add `additionalContext`, or rewrite the call
    (`permissionDecision: "allow"` with `updatedInput.command`).
  - PostToolUse can add `additionalContext`, or replace the tool result
    with `decision: "block"` and a reason.
  - `SessionStart` with `source: "compact"` runs after every compaction,
    before the next model request. `PreCompact` and `PostCompact` exist.
  - Hook output visible to the model is capped at about 2,500 tokens unless
    the handler sets `additionalContextLimit`.
  - Hooks inside code-mode JavaScript cells apply to the nested call.
- **Kiro hooks** (docs: kiro.dev/docs/hooks). The installed client is
  kiro-cli 2.37.0.
  - Standalone files `.kiro/hooks/<id>.json` (`{"version":"v1","hooks":[…]}`,
    PascalCase triggers) are the CLI V3 and IDE format. CLI 2.x uses hooks
    inside agent profiles. The installer must find out which one the
    installed client reads (see W4).
  - Command hooks get JSON on stdin (`hook_event_name`, `cwd`, `session_id`,
    `tool_name`, `tool_input`, and the result on PostToolUse). Exit 0 adds
    stdout to the agent's context. Any other exit code sends stderr to the
    agent, and on PreToolUse it blocks the tool.
  - Matchers: tool IDs, categories `read`/`write`/`shell`/`web`, `@mcp`,
    regex or wildcards. MCP tools appear as `mcp_<server>_<tool>`.
  - `PreCompact`/`PostCompact` need CLI 3.1.0 or later; 2.x has
    `agentSpawn` only.
  - MCP servers: `.kiro/settings/mcp.json`.
- **ozagia today** (at `05985ea`):
  - The Claude Code hook (`integrations/claude_code/hook.py`) has generic
    pieces: the daemon client, the fail-open main with a timeout and log,
    git ref refusal through `classify`, and node-key reads. The rest is
    Claude's tool names and envelope.
  - The daemon has no per-session state (`session` is passed to
    `intent.handle`/`after` and unused), no job table, and no autostart: it
    is started by hand or by the harness.
  - The daemon protocol already lists a `patch` tool with no handler.
  - Reads have no ledger and no outline; edit misses are handed to
    OpenCode's tolerant edit; there is no compaction handling anywhere.

## Rules for every workstream

- One git worktree per workstream: `~/ozmadev/ozagia-wt/<name>` on branch
  `feat/<name>`, from `main` at the start of the wave. Never edit the main
  checkout; another session has untracked files there (`.opencode/`).
- Follow `AGENTS.md`: standard library only in the core, edits through
  `edit/txn.py`, no writes into a target repo outside its state dir,
  nothing live (no model tokens) without a person's approval.
- Every behaviour change gets tests. The gate for a branch is
  `.venv/bin/python -m pytest -q` and `.venv/bin/ruff check src tests`
  passing in the worktree.
- Every feature adds a counter to `DaemonState.stats()` (and, where the
  eval reads stats, to `graph_stats_fields`/`aggregate`), so the live eval
  can measure it later.
- Agents commit on their branch with clear messages, and do not merge or
  push. The supervising session reviews, merges into `main`, runs the full
  checks again and pushes.
- Each agent writes a short report: what changed, tests added, what was
  not done, open questions.

## Wave 1: reach and cheap capture (parallel)

### W1. Hook core, daemon autostart and Codex integration (features 1, part of 4)

- Extract the generic parts of the Claude hook into
  `integrations/hookcore.py`: daemon client, fail-open main with timeout
  and log, git ref refusal, node-key detection, response flattening. The
  Claude hook keeps its behaviour; its tests must pass unchanged.
- `ozagia daemon ensure --root R`: start the daemon detached if no daemon
  answers on the socket, with an idle timeout (default 30 min, configurable)
  so hook-started daemons don't linger. Hooks call it lazily on first use,
  with a short time budget, and fail open.
- `integrations/codex/`: hook entry (`python -m ozagia.integrations.codex
  hook`) mapping:
  - `Bash` → `shell` (PreToolUse: git ref refusal; PostToolUse: `after`
    with the output, appended as `additionalContext`).
  - `apply_patch` → PostToolUse `after` for `patch` (graph refresh and
    delta lines). Patch-miss handling comes in W6.
  - `SessionStart`: a short brief (dirty files, recent commits, the MCP
    tools to prefer). The compaction working set comes in W8.
- `ozagia install codex [--scope project|user]`: idempotent merge into
  `.codex/hooks.json` (or `~/.codex/hooks.json`), an MCP entry under
  `[mcp_servers.ozagia]` in `.codex/config.toml` (or `~/.codex/config.toml`;
  write TOML conservatively: append or replace only ozagia's own table, keep
  everything else byte-identical), and an ozagia section in `AGENTS.md`
  between markers. `uninstall` reverses all three. Print the trust-review
  step the user must do in Codex.
- Docs: `docs/codex.md`. Tests mirror `tests/test_claude_hooks.py`
  (subprocess hook against a stub daemon; installer merge, idempotency,
  uninstall, malformed files).

### W2. More shell capture (feature 8, without `ps` and `jq`)

In `intent/shell.py`, `intent/run.py`, `intent/compound.py`:
- `rg`/`grep` over several paths; `rg --files` (with `-g`, `-t`, paths) as
  a glob; `rg --glob/-g`; `rg -C/-A/-B N` context lines.
- `nl -ba file` and `nl -ba file | sed -n 'a,bp'` as ranged reads; `tail -c N`.
- Environment-assignment prefixes on read-only commands (`LC_ALL=C rg …`).
- `timeout N <cmd>` around a mapped command.
- Subshell `( cd x && … )` treated as `cd` within the root; `cd ~` when
  `~` is inside the root.
- Pipe filters `| wc -l`, `| sort [-u]`, `| cut -d X -f N`, `| uniq`.
- `find` options seen in the corpus that are not mapped yet.
Keep refusing anything not provably read-only. Extend the `CLASSIFY` and
compound tables with the new forms and their refusals.

### W3. Structured data queries (feature 7)

- A read form `json:<file>#/a/b` (JSON pointer) and the same for YAML
  (only when the `iac-yaml` extra is installed) and TOML (`tomllib`):
  value, keys, length, slice, with a size cap and the node key.
- An outline for large JSON/YAML/TOML files (top-level keys, sizes, types,
  array lengths) on a read with no range, through the existing
  `outline_json`.
- Capture of a safe `jq` subset (`.a.b`, `.a[0]`, `.[N:M]`, `keys`,
  `length`, `.a | keys`, `-r`, `-c`, one file argument) as a shell class
  answered by the same code; everything else stays passthrough.
- MCP tool `dev_query` with the same semantics.

## Wave 2: context economy and waiting (parallel, after wave 1 is merged)

### W4. Kiro integration (feature 6)

On `hookcore`: `integrations/kiro/` hook entry, mapping Kiro's tool IDs
(`fs_read`/`read_file`, `fs_write`, `str_replace`, `execute_bash`,
`grep_search`, `file_search`, and their categories) to neutral tools.
- PreToolUse: git ref refusal (exit 2, reason on stderr); node-key reads.
- PostToolUse: `after` text on stdout.
- `agentSpawn`/`SessionStart`: the brief; `PostCompact` (3.1.0+): the
  working set from W8 once it exists.
- `ozagia install kiro`: writes `.kiro/hooks/ozagia.json` and the MCP entry
  in `.kiro/settings/mcp.json`; for CLI 2.x, also the hooks in the
  workspace agent profile if that is what 2.x reads. Find out which format
  the installed client loads from its own docs and files, and test both
  writers. Confirming the stdin shape against a live Kiro session needs a
  person: write a probe hook (dumps stdin to the state dir) and document
  how to run it, but don't run Kiro.
- Tool IDs and inputs: check them against `~/.kiro/sessions/*/messages.jsonl`
  (read only, record no content).

### W5. Per-session state, read ledger and outline-first reads (feature 2)

- Daemon per-session state keyed by `(harness, session)`, passed into
  `intent.handle`/`after`. Bounded (LRU of sessions, ranges per file).
- Ledger: for each read (built-in read, captured shell read, `sed -n`,
  `nl`, `head`, `tail`, `cat`), record file sha256 and line range.
- A new protocol op `session_event` (`compact`, `start`, `end`) clears the
  ledger for that session. Harnesses that report compaction (OpenCode's
  `compaction` hook, Codex `SessionStart:compact`/`PreCompact`, Claude
  `SessionStart:compact`, Kiro `PostCompact`) send it.
- Repeat policy, by config `[reads] repeat = "stub" | "note" | "off"`:
  - `stub`: an unchanged same-range repeat returns one line ("unchanged
    since read N, sha …, keys …") instead of the text. Only used for
    harnesses that report compaction; others get `note`.
  - `note`: the text plus an `[ozagia]` line saying it was unchanged.
- Outline-first: a read of a file over a threshold (default 400 lines or
  40 kB) with no range returns an outline (Python/Rust symbols, Markdown
  and TeX sections with line ranges and keys) plus the first chunk. Add
  Markdown and TeX section keys (`doc:<rel>#<slug>` or the existing
  `text:` ranges; pick one and document it) so a section can be read by key.
- Hooks: for Codex and Kiro, the stub is a PreToolUse deny with the stub as
  the reason; reads of a key go through the same path.
- Counters: repeats seen, stubs served, outlines served, bytes saved.

### W6. Patch and edit miss recovery (feature 3)

- `intent/patch.py`: parse the Codex/OpenAI patch format (`*** Begin
  Patch`, `*** Update File:`, `@@` anchors, ` `/`-`/`+` lines, `*** Add
  File`, `*** Delete File`, `*** Move to`). Check each hunk against the
  current file.
- Codex PreToolUse on `apply_patch`: if every hunk matches, allow. If a
  hunk fails, deny with, per failing hunk, the current text of the nearest
  region (fuzzy match on the expected lines, whitespace-tolerant), its
  line range, node key and file sha, capped. This replaces the failing call
  and the re-read that follows it.
- OpenCode edit miss and Kiro `str_replace` miss: the same nearest-region
  text in the `after`/error output.
- Key-addressed edits: `edit` with `key` (symbol, Markdown section, `text:`
  range) plus `base_sha256` replacing the node's text through `span_edit`,
  so an edit cannot apply to stale text silently. Expose it in MCP.
- Counters: patch checks, misses caught, region replies.

### W7. Background jobs and `wait` (feature 5)

- `daemon/jobs.py`: a job table (id, argv, cwd, pid, start, log path in the
  state dir, exit code). Start with `ozagia job run -- <cmd>` and MCP
  `job_start`. Jobs survive a hook call but not the daemon.
- `wait`: block until exit, a file appears, or a log line matches a regex,
  with a deadline; return only the new log lines (tail-capped) and the exit
  code. CLI `ozagia job wait`, MCP `job_wait`, `job_list`, `job_log`.
- `ps`/`pgrep` on a captured shell: answered from the job table when it
  names a known job, otherwise passthrough.
- A shell `sleep N` longer than a threshold followed by a check of a job's
  log: the `after` text points to `job_wait`.
- OpenCode: announce completion through `session.synthetic` if the plugin
  API allows it from a background callback.
- Counters: jobs, waits, wait seconds, polls avoided.

## Wave 3: working set and loop time (parallel, after wave 2)

### W8. Compaction working set and session brief (feature 4)

- From the per-session state (W5) and the journals: files and keys
  edited, tests last failing, open jobs, the last plan step if a
  `DevSession` exists. Bounded to about 2,000 tokens.
- Delivered on every harness's compaction event (OpenCode `compaction`
  hook, Codex `SessionStart:compact`, Claude `SessionStart:compact`, Kiro
  `PostCompact`), and as MCP `dev_working_set`. The session-start brief
  reuses the same builder with "since the last session" changes.

### W9. Affected tests and a test-result cache (feature 9)

- A captured pytest with no selector: run tests linked to the keys changed
  since the last green run first (graph `covering_tests` and the impact
  walk), report the rest as not run, with the full command to run them.
  Opt-out with `--ozagia-all` or config.
- Cache: a pass keyed by the hashes of the test file and the files it
  imports (from the graph); an unchanged passing test is reported as cached
  instead of rerun. Slow and flaky tests remembered.
- Same selection for `cargo test` where the Rust graph allows it.

### W10. Lint inside the edit (feature 10)

After a handled edit, run the configured linter on the changed file (ruff
if `pyproject.toml`/`.ozagia.toml` names it, `rustfmt --check` for Rust),
with a short timeout, and report findings for the changed keys in the
`[ozagia]` lines. Capture `ruff check …` and `.venv/bin/ruff …` as a
mapped command.

### W11. Path resolution and cross-session ownership (features 12, 13)

- A read or edit of a missing path: answer with the nearest graph files
  (same directory, fuzzy name, the same relative path in the main checkout
  or another worktree of the repository).
- Record the session in DevMemory's edit journal. Before an edit, warn when
  another live session has uncommitted edits to the same file. Query
  `ozagia who <path>` and MCP `dev_who`.

## Wave 4: scope (parallel, after wave 3)

- **W12. Multi-file replace** (feature 11): preview-first, scoped by glob or
  keys, through the edit transaction, returning per-file deltas.
- **W13. Operations graph** (feature 14): compose files, Containerfiles,
  quadlets, systemd units; map `podman logs`/`journalctl` output to nodes.
- **W14. Research user positions and review closure** (feature 15).
- **W15. Fetch fallback** (feature 16).

## Measurement

Offline checks run in every branch. The live agent eval (`ozagia.eval.agent
--live`, `OZAGIA_EVAL_LIVE=1`) spends model tokens and needs a person's
approval. After each wave, propose one live run: baseline vs graph_intent
on the same tasks, comparing tokens per task, tool calls, compactions,
patch failures and wall time at the same success rate.

## Risks

- Hooks that deny reads (`stub`) can hide text the model has lost. The
  stub is only used where the harness reports compaction, and the eval must
  show no drop in success rate before it is the default.
- Codex and Kiro hook formats change between releases. Installers pin the
  formats they write, and `ozagia doctor` reports when the installed client
  version is outside the tested range.
- Several workstreams touch `daemon/server.py` and `intent/__init__.py`.
  Waves keep those in separate merges; conflicts are resolved by the
  supervising session.
