# Unified status snapshot

`python -m ozagia.status_cli` returns one read-only snapshot of a repository:
workspace and worktrees, host resources, the created-file ledger, runtime
services and tunnels, the graph daemon, diagnostics, project configuration and
the newest deep-review manifest.

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
| `doctor` | `ozagia.doctor.diagnose` | Config validity, graph snapshot freshness, parsers, language servers, optional tools |
| `reviews` | `<root>/.ozagia/reviews/*/manifest.json` | Newest deep-review directory, run status, `complete`/`exhaustive`/`truth_assessed` flags, finding and subject counts, bounded subject statuses |
| `config` | `ozagia.config` | Resolved config path, adapters, index directories, jobs flag, configured runtime names |

Every section is an envelope:

```json
{
  "state": "ok",
  "error": null,
  "report": {}
}
```

`report` is the source module's result, unmodified except where a section
summarizes a stored artifact (the bounded `reviews` summary) or preserves every
`runtime_status` value. On an unavailable component the envelope still exists
and `report` is `null`; it is never an empty object. A section that can still
name what is missing keeps its partial report (daemon and reviews).

`reviews.report` contains `root` (the reviewed repository), `reviews_root`,
`latest`, `manifest`, `manifest_root`, `age_s` when a manifest was read,
`status`, `complete`, `exhaustive`, `truth_assessed`, `findings`,
`subject_count`, a bounded `subjects` status map and `subjects_truncated`.
Finding IDs, evidence and subject report contents are not copied.

## States

Section states describe whether the section could be read, not whether the
repository is healthy.

| State | Meaning |
| --- | --- |
| `ok` | The component was read completely (for `reviews`, a valid manifest was read) |
| `degraded` | It was read, but a secondary fact is missing or stale (currently the graph snapshot) |
| `unavailable` | It could not be read; `error` says why |
| `absent` | It is known not to exist (daemon: nothing listens on the socket; reviews: no review directory or no `deep-*` directory) |
| `unknown` | It exists or may exist but could not be determined (daemon timeout or bad reply; reviews: the newest manifest is missing, unreadable, malformed, ambiguous or outside the reviews directory) |
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
- `snapshot.missing`, `snapshot.stale`, `snapshot.invalid` or
  `snapshot.unknown`: the graph snapshot's state from `doctor`;
- `reviews.manifest`, `reviews.unreadable`, `reviews.ambiguous` or
  `reviews.escape`: the newest deep-review manifest is missing, malformed,
  unreadable, ambiguous or reached through a symlink or outside the reviews
  directory.

The `degraded` list uses `{"component", "code", "message"}` objects, as does
`errors`.

## Absent versus unknown

Absence is a fact; ignorance is not absence. The snapshot keeps them apart:

- **Daemon:** a missing socket or connection refused is `absent`. A timeout,
  bad reply or closed connection is `unknown`. `ping` and `stats` are `null`,
  not `{}`.
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

## Read-only guarantees

The composition never:

- writes to the repository, its state directory or the daemon socket;
- creates `.ozagia/` (a missing state directory is reported, not created);
- creates, refreshes, prunes, rewrites or deletes deep-review directories or
  their `manifest.json`;
- builds or updates a graph snapshot;
- starts, stops, signals or autostarts a daemon, service, tunnel or job;
- starts a language server, relay, cloud or cluster command.

It reads git metadata, `/proc`, the ledger, runtime state, project config and
the newest deep-review manifest, and it pings an existing daemon socket when
one is present. The real-repository test asserts that no `.ozagia/` directory
appears after a run.

## Scope

This is the first slice of plan item 6. It covers workspace, resources, files,
runtime processes, the daemon, diagnostics and the newest deep-review manifest.
Per-session and per-job usage, admission refusals, background jobs,
connections/providers beyond the daemon, capability probes beyond `doctor`, and
snapshot size metrics are not part of this shape yet; they can be added as
extra sections without changing the envelope or the absence-versus-unknown
rule.
