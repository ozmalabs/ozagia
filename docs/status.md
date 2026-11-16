# Unified status snapshot

`python -m ozagia.status_cli` returns one read-only snapshot of a repository:
workspace and worktrees, host resources, the created-file ledger, runtime
services and tunnels, the graph daemon, diagnostics and project configuration.

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
| `config` | `ozagia.config` | Resolved config path, adapters, index directories, jobs flag, configured runtime names |

Every section is an envelope:

```json
{
  "state": "ok",
  "error": null,
  "report": {}
}
```

`report` is the source module's result, unmodified except that the runtime
section preserves every `runtime_status` value. On a failed component the
envelope still exists and `report` is `null`; it is never an empty object.

## States

Section states describe whether the section could be read, not whether the
repository is healthy.

| State | Meaning |
| --- | --- |
| `ok` | The component was read completely |
| `degraded` | It was read, but a secondary fact is missing or stale (currently the graph snapshot) |
| `unavailable` | It could not be read; `error` says why |
| `absent` | It is known not to exist (daemon: nothing listens on the socket) |
| `unknown` | It exists or may exist but could not be determined (daemon timeout or bad reply) |
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
  `snapshot.unknown`: the graph snapshot's state from `doctor`.

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

## Read-only guarantees

The composition never:

- writes to the repository, its state directory or the daemon socket;
- creates `.ozagia/` (a missing state directory is reported, not created);
- builds or updates a graph snapshot;
- starts, stops, signals or autostarts a daemon, service, tunnel or job;
- starts a language server, relay, cloud or cluster command.

It reads git metadata, `/proc`, the ledger, runtime state and project config,
and it pings an existing daemon socket when one is present. The real-repository
test asserts that no `.ozagia/` directory appears after a run.

## Scope

This is the first slice of plan item 6. It covers workspace, resources, files,
runtime processes, the daemon and diagnostics. Per-session and per-job usage,
admission refusals, background jobs, connections/providers beyond the daemon,
capability probes beyond `doctor`, and snapshot size metrics are not part of
this shape yet; they can be added as extra sections without changing the
envelope or the absence-versus-unknown rule.
