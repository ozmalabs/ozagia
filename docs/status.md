# Unified status snapshot

`python -m ozagia.status_cli` returns one read-only snapshot of a repository:
workspace and worktrees, host resources, the created-file ledger, runtime
services and tunnels, the graph daemon, its jobs and admission counters,
secret-provider readiness, diagnostics, snapshot size/counts, metadata-only
session profiles, cross-session dirty-file ownership, local session-mesh
metadata, project configuration and the newest deep-review manifest.

It composes checks that already exist. It is not a second service manager and
it does not redefine runtime states. A configured service is `configured`, not
running. A persisted tunnel lease that this process does not hold is `stale`,
not connected. A stale graph snapshot is never reported as fresh.

```bash
PYTHONPATH=src python -m ozagia.status_cli [--root R] [--path P] [--top N] [--json]
```

`--path` also observes another filesystem in the resource section. `--top`
bounds the host process-group list (default 10). The daemon probe uses a
two-second timeout.

Exit status is `0` when the snapshot was composed, `1` when it contains a hard
error, and `2` for a usage or unhandled read error. A degraded component is
reported but does not by itself fail the command.

## What is composed

| Section | Source | What it contains |
| --- | --- | --- |
| `workspace` | `ozagia.core.worktree.check` | Worktrees, branches, expected branch, ahead/behind base and upstream, dirty/untracked/conflicted counts, in-progress operation, findings |
| `resources` | `ozagia.core.resources.status` | Host memory, disk, load, pressure, top process groups, policy and observed history |
| `files` | `ozagia.core.files` | Ledger records, orphan count, new-file count, ledger path and presence |
| `runtime` | `ozagia.core.runtime.runtime_status` | Services, tunnels and raw processes with their runtime states, relays and context lines |
| `daemon` | `ozagia.daemon.client` | Ping, pid, uptime, idle timeout and daemon stats |
| `jobs` | `ozagia.daemon.client.job_list` | Bounded rows of the running daemon's job table: id, state, pid, process facts, exit/signal, elapsed, peak memory/disk and log size; command/argv/paths/log text are not copied |
| `admissions` | daemon stats (`stats.jobs`) | Job admission and resource refusals, resource kills/events and peak use from the running daemon's job counters |
| `providers` | daemon `secrets` op, `action=providers` | Secret-provider availability/readiness from the running daemon; not a model connection and never inferred from configuration |
| `doctor` | `ozagia.doctor.diagnose` | Config validity, graph snapshot freshness, parsers, language servers, optional tools, host integrations and a bounded capability summary |
| `integrations` | `ozagia.doctor.diagnose` | Bounded host version states and read-only integration configuration presence; no host agent is started |
| `capabilities` | `ozagia.doctor.diagnose` | Catalogue version/hash/counts and the bounded result of the read-only capability audit; full findings are not copied |
| `snapshot` | `ozagia.doctor.diagnose` plus snapshot `stat` | Snapshot path, size, mtime/age, freshness and node/edge counts when doctor's existing snapshot load exposes them; no build or rewrite |
| `sessions` | `<root>/.ozagia/session_profiles/*.json` | Bounded metadata summary: profile count, newest age, model names, token/tool/phase counters and capture diagnostics; no prompt, path, tool body, source content or full session id |
| `ownership` | `ozagia.core.owners` and the edit/daemon journals | Bounded dirty files with uncommitted journaled edits by a session, liveness and age; only files a session journaled are attributed |
| `reviews` | `<root>/.ozagia/reviews/*/manifest.json` | Newest deep-review directory, run status, `complete`/`exhaustive`/`truth_assessed` flags, finding and subject counts, bounded subject statuses |
| `mesh` | `ozagia.core.mesh` under `<root>/.ozagia/mesh/` | Read-only local session-mesh metadata: counts, self, peers, links/roles, leases, delegations and unread. No message body, subject or daemon |
| `flight` | `ozagia.core.ozma_bridge` under `<root>/.ozagia/mesh/ozma/` | Compact read-only Ozma flight counts: members, roles/workunits from host agent ids only, links/delegations, advisory claim/blocker counts, handoff and wake counts plus declared readiness. No member rows, bodies or credentials |
| `config` | `ozagia.config` | Resolved config path, adapters, index directories, jobs flag, configured runtime names |

Every section is an envelope:

```json
{
  "state": "ok",
  "error": null,
  "report": {}
}
```

`report` is the source module's result, unmodified for the pass-through
sections (`workspace`, `resources`, `files`, `runtime`, `daemon`, `doctor` and
`config`) and a bounded extraction for `integrations`, `capabilities`,
`reviews`, `jobs`, `admissions`, `providers`, `snapshot`, `sessions`,
`ownership`, `mesh` and `flight`. On an unavailable component the envelope
still exists and `report` is `null`; it is never an empty object. A section that can still
name what is missing keeps its partial report (daemon, reviews, jobs, providers,
snapshot, sessions, ownership, mesh and flight).

`reviews.report` contains `root` (the reviewed repository), `reviews_root`,
`latest`, `manifest`, `manifest_root`, `age_s` when a manifest was read,
`status`, `complete`, `exhaustive`, `truth_assessed`, `findings`,
`subject_count`, a bounded `subjects` status map and `subjects_truncated`.
Finding IDs, evidence and subject report contents are not copied.

`integrations.report` is a bounded extraction of `doctor.report.integrations`:
`note`, `hosts` (one row per host with `state`, `installed`, `version`, `path`,
`tested_range`, `verification` and `error`), `config` (one `state`, `found` and
`errors` list per integration) and `warnings`. The supported host states are
`verified`, `installed_untested_version`, `not_installed` and `unknown`. A
missing binary is `not_installed`; a binary whose `--version` timed out or
could not be parsed is `unknown`. Configuration presence is reported separately
and never means the host loads or trusts the integration. The host rows, tested
versions and evidence are in [integrations.md](integrations.md).

`capabilities.report` contains `state`, `catalogue` (version, hash, capability,
domain and surface counts), `audit` (state, fixed count keys and finding counts
by kind/severity), `warnings` and `error`. The audit's finding list, evidence
and graph are not copied. Capability findings and drift are warnings; they do
not make the status command fail.

`jobs.report` contains `socket`, `timeout_s`, `source`, `jobs`, `counts`
(`known`, `running`, `shown`) and `truncated`. A row carries `id`, `state`,
`pid`, `started`, `ended`, `exit_code`, `signal`, `detach`, `harness`, a short
session `label`, `enforcement`, `peak_memory_mb`, `peak_disk_write_mb`,
`log_bytes`, `elapsed_s`, `argv_count`, `has_command` and the process-group
flags. At most 50 rows are copied. Command text, argv values, `cwd`, environment
values, log paths and log contents are deliberately omitted: a command line can
carry a value that was never resolved through the daemon broker.

`admissions.report` contains `source` and `counts`: `jobs_refused`,
`resource_refusals`, `jobs_resource_killed`, `resource_events`,
`cgroup_oom_kills`, `stopped_on_load`, `resource_peak_mb` and
`resource_peak_disk_write_mb`. These are the running daemon's job counters, not
a new admission probe.

`providers.report` contains `socket`, `timeout_s`, `source`, `kind`
(`secret_providers`), `note`, bounded `providers` rows, `counts` (`known`,
`ready`, `shown`) and `truncated`. A row carries `name`, `enabled`, `available`,
`ready`, bounded `requires` and `notes`. This is secret-provider readiness in
the running daemon, not a model/network connection; configuration alone is not
reported as a connection.

`snapshot.report` contains `path`, `freshness`, `size_bytes`, `mtime`, `age_s`,
`nodes`, `edges`, `counts_read` and `counts_note`. Counts come from the
snapshot load `doctor` already performs; status does not load, build or rewrite
the graph. At most 50 job rows, 20 providers, 50 session profiles and 32
ownership rows are copied.

`sessions.report` contains `dir`, `profiles`, `profiles_read`, `malformed`,
`unreadable`, `truncated`, `newest_age_s`, `models`, `totals`, an aggregated
`capture_diagnostics` map and bounded `sessions` rows. A row carries `harness`,
a short session `label`, `observed_at`, `age_s`, `source`, `model_calls`,
`models`, `tokens`, `timing_ms`, `tool_calls`, `coverage` and capture counters.
At most 200 profile files are scanned, 50 are read (each at most 32 KiB), 20
model names are kept and capture snapshots are deduplicated by `capture_id`.
Prompts, paths, tool input/output, source content, full session ids and
`seen_*` id lists are not copied.

`ownership.report` contains `journal`, `files_scanned`, `counts` (`files`,
`sessions`, `rows`), bounded `rows` and `truncated`. A row carries `path`,
`harness`, a short session `label`, `edits`, `last`, `last_age`, `live` and
`committed=false`. It reads the last 2000 journaled edits, checks at most 16
newest dirty candidate files and copies at most 32 rows. Only files that a
session journaled are attributed; a dirty file with no journal entry is not
guessed.

`mesh.report` contains `mesh_id`, `repo_id`, `counts` (sessions, leases, links,
delegations), `self` (resolved from `OZAGIA_MESH_HARNESS`/`OZAGIA_MESH_SESSION`
when set), bounded `peers`, `links` with derived relations, `leases` with
resource/mode/expiry/queue and `holder_is_self`, bounded `delegations`,
`unread` and mesh read errors. Message bodies, subjects, reasons, history and
credentials are not copied. The section reads the same filesystem registry as
`ozagia mesh status`; it never creates `.ozagia/mesh`, starts a daemon, opens a
network connection or changes a lease.

`flight.report` contains `flight_id`, `source`, `counts` (members, roles,
workunits, links, delegations, active claims, claim conflicts, open blockers,
pending handoffs and pending wake requests), `ready`, `conformance`, `verified`
and `network`. It is counts only: member rows, message bodies, criteria text,
credentials and card paths are not copied. The section is `absent` when the
bridge is neither configured nor enabled; otherwise it reads the same local
stores as `ozagia mesh ozma flight` and never creates them, opens a socket or
changes a claim, blocker, handoff or wake record.

## States

Section states describe whether the section could be read, not whether the
repository is healthy.

| State | Meaning |
| --- | --- |
| `ok` | The component was read completely (for `reviews`, a valid manifest was read) |
| `degraded` | It was read, but a secondary fact is missing or stale (the graph snapshot, or the snapshot metrics section when its manifest is stale) |
| `unavailable` | It could not be read; `error` says why |
| `absent` | It is known not to exist (daemon: nothing listens on the socket; reviews: no review directory or no `deep-*` directory; sessions: no profile directory; ownership: no edit journal; mesh: no mesh state and no `[mesh]` configuration; flight: no `[ozma_mesh]` configuration or enabled bridge; snapshot: no snapshot file) |
| `unknown` | It exists or may exist but could not be determined (daemon timeout or bad reply; jobs/providers: failed daemon reply; sessions: unreadable or malformed profile; ownership: unreadable journal; mesh: unreadable or invalid mesh state/configuration; flight: unreadable or invalid `[ozma_mesh]` configuration or flight store; reviews: the newest manifest is missing, unreadable, malformed, ambiguous or outside the reviews directory) |
| `configured` | Project configuration file was loaded |
| `defaults` | No project configuration file; built-in defaults apply |
| `invalid` | Project configuration is malformed |

Health details stay in `report`: `workspace.report.ok` and
`workspace.report.findings` are the worktree check's result, and runtime item
states are the values from `runtime_status`.

## Hard errors and degraded states

A hard error means the snapshot itself cannot be trusted:

- the repository root does not exist (`root.missing`);
- project configuration is malformed or an `OZAGIA_CONFIG` path is missing
  (`config.invalid`).

`errors` lists those; `ok` is false and the CLI exits `1`. Everything else is
degraded:

- `workspace.unavailable`: git or the worktree configuration could not be read;
- `resources.unavailable`: host observation failed;
- `files.unavailable`: the ledger could not be read;
- `runtime.unavailable`: runtime state could not be composed;
- `daemon.absent` or `daemon.unknown`: no daemon, or it did not answer;
- `daemon.stats`: ping worked but stats did not;
- `jobs.unknown`: the daemon answered ping but its job table could not be read;
- `providers.unknown`: the daemon answered ping but provider readiness could
  not be read;
- `sessions.unreadable` or `sessions.malformed`: the profile directory could
  not be listed or contains unreadable/malformed profiles; a partial summary of
  the readable profiles is still reported;
- `ownership.unreadable`: the edit journal is not a regular file or could not
  be read;
- `snapshot.unreadable`: the snapshot file could not be `stat`ed;
- `snapshot.missing`, `snapshot.stale`, `snapshot.invalid` or
  `snapshot.unknown`: the graph snapshot's state from `doctor`;
- `reviews.manifest`, `reviews.unreadable`, `reviews.ambiguous` or
  `reviews.escape`: the newest deep-review manifest is missing, malformed,
  unreadable, ambiguous or reached through a symlink or outside the reviews
  directory.

Absent sections are facts rather than degradation: no daemon means `jobs`,
`admissions` and `providers` are `absent`; no profile directory means
`sessions` is `absent`; no edit journal means `ownership` is `absent`; no mesh
state and no `[mesh]` configuration means `mesh` is `absent`; no snapshot file
means `snapshot` is `absent`. An empty profile directory is `ok`
with zero profiles, which is not the same as a missing directory.

The `degraded` list uses `{"component", "code", "message"}` objects, as does
`errors`.

## Absent versus unknown

Absence is a fact; ignorance is not absence. The snapshot keeps them apart:

- **Daemon:** a missing socket or connection refused is `absent`. A timeout,
  bad reply or closed connection is `unknown`. `ping` and `stats` are `null`,
  not `{}`.
- **Jobs, admissions, providers:** with no daemon they are `absent` and report
  is a null/bounded shape, not an empty object. When the daemon answered ping
  but the job table, job counters or provider reply failed, they are `unknown`
  with the error. Jobs are listed only; none is started, waited on, attached to
  or signalled. Provider rows are the daemon's secret-provider readiness, not a
  model connection.
- **Sessions:** no `.ozagia/session_profiles/` is `absent`; a directory with no
  `*.json` files is `ok` with zero profiles. An unreadable directory, a symlink
  or a malformed/oversized profile is `unknown`; readable profiles still
  contribute to the partial summary and the error names the count of bad files.
  A profile with no host or daemon counters is still a profile, with null/zero
  counters rather than invented values.
- **Snapshot metrics:** the section reads the file `stat` and the freshness and
  counts that `doctor` already produced. `missing` is `absent`; `invalid` or
  `unknown` freshness is `unknown`; `stale` is `degraded`; `fresh` is `ok`.
  When counts are unavailable, `counts_read` is false and `counts_note` says
  so; size, mtime, age and freshness are still reported.
- **Ownership:** no edit journal is `absent`. An unreadable or non-regular
  journal is `unknown`. Only journaled edits are considered, and only when the
  file is dirty and the edit is uncommitted; a dirty file with no journal entry
  is not attributed. Liveness comes from the daemon journal's session window
  and running MCP pids, never from a new probe.
- **Snapshot:** `doctor` reports `missing`, `fresh`, `stale`, `invalid` or
  `unknown`. A missing snapshot says so and suggests building one; an invalid
  one includes the error.
- **File ledger:** `files.report.ledger.present` is false when there is no
  ledger file. Zero records is not the same as no ledger.
- **Configuration:** `defaults` means no file and defaults apply. `invalid`
  means a file exists but could not be parsed.
- **Resources:** unreadable host values stay `null` inside the resource report
  rather than being guessed.
- **Deep reviews:** no `.ozagia/reviews/` or no `deep-*` directory in it is
  `absent`; nothing is created. A `deep-*` directory without a readable
  `manifest.json`, with invalid JSON or schema, over the 1 MB manifest limit,
  with more than 256 candidates, through a symlinked state/reviews/review path,
  or resolving outside the reviews directory is `unknown`. The section reports
  `latest` and an error instead of inventing a report. A partial run is still
  `ok`: `status`, `complete`, `exhaustive` and `truth_assessed` copy what the
  manifest says. Only the newest manifest is read; findings are not copied and
  at most 32 subject statuses are shown.
- **Integrations:** a host binary not found on `PATH` (or the project/ozagia
  `.venv` for pyright) is `not_installed`; a found binary whose bounded
  `--version` call times out, cannot run or prints no version is `unknown`. An
  installed version not in the matrix's tested versions is
  `installed_untested_version`. Config files are `present`, `absent` or
  `unknown` (unreadable or malformed). None of these states is a live-session
  result.
- **Capabilities:** the section is `ok` when the bounded audit report was
  produced. Catalogue drift and audit findings stay in `warnings`; they are not
  presented as verified coverage and do not become a hard error.

## Rust tier declaration

The Rust tier is declared in [`rust.md`](rust.md) and in the capability
catalogue (`cap:languages.rust_tier`). It is syntactic by default:

| Layer | Declared limit |
| --- | --- |
| tags | tree-sitter structural query plus regex fallback; no macro/generated/cfg expansion |
| symbols | definitions, references, spans, docs and Test nodes; no type inference or trait resolution |
| imports | `mod`/`use` resolution for local and workspace crates; external and ambiguous bindings unresolved |
| edits | preview-first transaction, parse gate, same-file rename; cross-file rename is an LSP preview |
| lsp | optional rust-analyzer; indexing latency and install status are reported |
| tests | captured `cargo test`, target-level affected/cache; stable human output only; failures never cached |

SCIP is an optional importer, not a snapshot mutation. The tier benchmark and
SCIP fixture result are recorded under
[`docs/results/`](results/rust_parity_bench_2026-11-23.md). Rust
trust-boundary/dataflow analysis is a separate plan.

## Read-only guarantees

The composition never:

- writes to the repository, its state directory or the daemon socket;
- creates `.ozagia/` (a missing state directory is reported, not created);
- creates, refreshes, prunes, rewrites or deletes deep-review directories or
  their `manifest.json`;
- builds, updates, refreshes or rewrites a graph snapshot;
- starts, stops, signals or autostarts a daemon, service, tunnel or job;
- starts, waits on, attaches to or stops a job;
- resolves a secret value or writes a session profile;
- starts a language server, relay, cloud or cluster command;
- starts a host agent session or makes a model/tool/network call. Doctor may
  run an installed host binary with `--version` under a two-second timeout;
  that is the only host process the snapshot starts.

It reads git metadata, `/proc`, the ledger, runtime state, project config, the
newest deep-review manifest, the session-profile directory, the edit and daemon
journals and the snapshot's `stat`, and it pings an existing daemon socket when
one is present. With a running daemon it also asks for `stats`, `job_list` and
the secret-provider list. Those are read requests to the existing process; no
job is started or attached to and no secret is resolved. Doctor already loads
the snapshot to judge freshness, and the snapshot section reuses its node/edge
counts instead of loading the graph a second time. The real-repository test
asserts that no `.ozagia/` directory appears after a run.

## Scope

This is the second slice of plan item 6. It covers workspace, resources, files,
runtime processes, the daemon, jobs, admission/resource refusals,
secret-provider readiness, diagnostics, host integrations, the capability
catalogue summary, snapshot size/counts, metadata-only session profiles,
dirty-file ownership and the newest deep-review manifest.

Still outside this read-only shape:

- the native session-secret UI, prompt rewrite and revoke/expiry flow;
- complete tool-lifecycle capture for Codex, Kiro and Claude Code (the
  sessions section reports whatever profiles those hosts actually wrote and
  does not claim coverage they lack);
- model-provider **connections** (the providers section is secret-provider
  readiness from the daemon, not a live model/network probe);
- live integration sessions, capability probes beyond the existing audit and
  graph builds or rewrites.

Those remain 0.3/0.4 work. New sections keep the same `{state, error, report}`
envelope and the same absent-versus-unknown rule.
