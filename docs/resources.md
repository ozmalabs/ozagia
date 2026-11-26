# Host resources, admission checks and enforcement

`ozagia-resources` observes the host and answers whether a job, task or
worktree is likely to fit. The observation commands are read-only. The daemon's
background jobs use kernel enforcement where possible: on Linux with a
delegated cgroup v2 hierarchy, each job gets its own `ozagia/job-<id>` cgroup
with `memory.max` and `pids.max`, plus child rlimits and a verified-process-group
watchdog. Disk writes are attributed from the cgroup's `io.stat`, falling back
to the process group's `/proc/<pid>/io`, so the `disk_mb` budget is enforced
against bytes this job wrote where Linux exposes them. Without delegation, a
permission failure or a non-Linux host, the job falls back to `rlimit+watch` and
the record names the reason. The watchdog only signals processes ozagia started,
and only while the job's process group is verified; `cgroup.kill` reaches only
the job's own cgroup.

Resource grants are separate from host admission. A grant declares which VM,
host or cloud account a task may use, its budget, approval requirement and
cleanup policy; admission still answers whether a run fits on this host now.
Grant config is read only from `[resource_grants.<id>]` in
`ozagia/resources.toml` or `ozagia/config.toml` and is never merged into the
host policy or `status`. See [resource_grants.md](resource_grants.md) for the
schema, examples and the read-only `grants list|show|check` commands.

## What is observed

- **Memory:** total and available memory, swap, used percentage. Linux reads
  `/proc/meminfo`; other platforms use the optional `psutil` extra or
  `sysconf` and report `unknown` rather than guessing.
- **Disk:** for a path, the mount point, filesystem type, free bytes, free
  inodes and `is_tmpfs`. A worktree under `/tmp` on tmpfs consumes memory, not
  disk.
- **CPU/load:** one/five/fifteen-minute load averages and the CPU count.
- **Pressure:** Linux `/proc/pressure/{cpu,memory,io}` when available.
- **Process groups:** RSS and attributed storage I/O per process group, and
  the largest groups by RSS. These are host-wide observations; they are not
  automatically attributed to a session. Only the executable name is reported,
  never the argument list, which may contain secrets. Group I/O is the sum of
  the readable members' `/proc/<pid>/io` `read_bytes`/`write_bytes`; a member
  whose file is unreadable is skipped, and `disk_source` is `proc.io` only
  when at least one member was readable.
- **History:** a bounded JSONL store under `.ozagia/resources/history.jsonl`
  with p50/p90/max memory, disk, duration and exit codes per key.

The history key is either a task class or `command_key(argv)`, which stores the
command head and a hash, never raw arguments. It is intended for expected-use
estimates, not for auditing commands.

## Disk write attribution

A job's writes are read from the kernel, not inferred from file sizes:

| Source | Where | Fields | Notes |
| --- | --- | --- | --- |
| cgroup | `<job cgroup>/io.stat` | `io_read_mb`, `io_write_mb`, `io_source: "cgroup.io.stat"`, bounded `io_devices` | Preferred. Summed over devices. Missing file is `None`, not an error. |
| process group | `/proc/<pid>/io` for each group member | `disk_read_mb`, `disk_write_mb`, `disk_source: "proc.io"` | Fallback. Readable members are summed; unreadable members are skipped. |
| filesystem | `statvfs` on the job's cwd | `disk_used_mb` (free-space drop) | Host pressure only; used for the reserve and as the `disk_mb` fallback when neither attribution source is readable. |

`read_bytes`/`write_bytes` and `io.stat`'s `rbytes`/`wbytes` count bytes the
kernel sent to or fetched from storage. They are not allocated blocks:
overwrites count again, cached writes may not have reached storage yet, and
I/O through a path ozagia cannot attribute (another process, another cgroup)
is not included. The cgroup value wins when both exist, and the final
`io.stat` read before cleanup can raise the job's `peak_disk_write_mb`. The
process-group fallback sums the members alive at that sample, so a very short
child that exits between samples may be missed; a cgroup's cumulative
`io.stat` does not have that gap.

A watchdog sample records `disk_write_mb` (the current attributed total),
`peak_disk_write_mb`, `disk_source`, `disk_free_gb` and `disk_used_mb`. When
`disk_mb` is set and attribution is present, the budget is checked against the
attributed writes and the breach event is `kind: "disk_write"` with
`budget_mb`, `observed_mb` and `source`. When attribution is absent, the same
budget uses the free-space drop and reports `kind: "disk"`; a reserve breach
also stays `kind: "disk"`. One breach produces one event.

## Commands

```text
ozagia-resources status [--path PATH] [--top N] [--json]
ozagia-resources check --for job|task|worktree [options] [--json]
ozagia-resources history [--key KEY] [--json]
ozagia-resources explain --for job|task|worktree [options]
ozagia-resources grants list [--kind K] [--json]
ozagia-resources grants show ID [--json]
ozagia-resources grants check ID [--live] [--probe-versions] [--require-ready] [--json]
```

Admission options:

```text
--memory-mb N        requested memory
--disk-mb N          requested disk
--path PATH          filesystem to check (default: repo root)
--key KEY            history key for the expected p90
--freeing MEM:ETA    an ozagia job expected to free memory
```

Exit status is `0` when the request fits and `1` when it is refused. `2` is a
usage or read error.

## Policy

Defaults live in code; a repository may override them in
`ozagia/resources.toml` or `ozagia/config.toml` under `[resources]`.

```toml
[resources]
enabled = true
sample_s = 2
watchdog = "auto"          # auto, cgroup, rlimit, watch, off
reserve_memory_pct = 10
reserve_disk_pct = 0         # optional; 0 = use the absolute reserve
reserve_disk_gb = 1
history = true

[resources.limits]
job_memory_mb = 2048
job_pids = 256
job_disk_mb = 4096
# job_wall_s = 3600       # optional: stop a job after this long
# job_file_mb = 512       # optional: RLIMIT_FSIZE, per file

[resources.admission]
min_free_memory_pct = 15
min_free_disk_gb = 2
wait_max_s = 300

[resources.worktrees]
root = "../ozagia-wt"
min_free_gb = 5
avoid_tmpfs = true
```

The checks subtract the OS reserve from available memory and free disk. Disk
admission is based on the request (or its historical p90), an absolute reserve
and an absolute free-space floor; a low free-space percentage alone is not a
refusal. A host already below an absolute minimum is refused even for a small
request. A request that does not fit returns the shortfall, the expected p90
and suggestions. `min_free_disk_pct` was removed: use `min_free_disk_gb` for
the host floor and the requested/expected `disk_mb` for the work.
`enabled = false` turns admission checks into a reported no-op and disables job
enforcement. `watchdog = "off"` keeps admission but turns enforcement off.
`auto` (the default) and `cgroup` prefer a delegated cgroup v2 hierarchy and
fall back to `rlimit+watch`; `rlimit` and `watch` skip cgroups (rlimits still
apply, and the watchdog is the only kill mechanism). A configuration edit is
read when the daemon starts; restart it after changing the policy.
`OZAGIA_CGROUPS=0` in the daemon's environment is an emergency opt-out from
cgroup attempts without changing the rest of the policy.

If another ozagia job is expected to free enough within the wait window,
`retry_after_s` is set and the caller can retry or wait. This build does not
wait silently.

## Enforcement

Limits are chosen from `[resources.limits]` when a job starts. A caller can
also pass explicit limits to `JobTable.start` (`memory_mb`, `pids`, `disk_mb`,
`wall_s`, `file_mb`); `None` in a field means "not limited".

Admission runs before a process is spawned when the job has a memory or disk
budget. A refusal starts nothing, creates no log and returns a structured
error with `reason`, `requested`, `available`, `shortfall`, `retry_after_s`,
`suggestions` and the full admission report. The same refusal is visible
through the job protocol as `ok: false`, `resource_refused: true`.

Mechanism selection happens when the job starts:

- `cgroup+rlimit` when cgroup v2 is delegated and the policy mode is `auto` or
  `cgroup`;
- `rlimit+watch` when cgroups are absent, not delegated, refused by
  permissions, or the mode is `rlimit`/`watch`;
- `off` when resources are disabled or `watchdog = "off"`.

A cgroup failure never fails the job. The record's `cgroup` object carries
`status` (`active`, `fallback` or `off`), `attempted`, `used`, `path`, `limits`,
`peak_memory_mb`, `oom_events`, `oom_kill`, `fallback`, `reason` and
`cleaned_up`, so a session can see why the fallback happened. The daemon counts
`cgroup_jobs`, `cgroup_fallbacks` and `cgroup_oom_kills` in `stats`.

**cgroup v2 (`core.cgroup`).** On Linux the daemon detects cgroup v2 at
`/sys/fs/cgroup` by reading `cgroup.controllers`, `cgroup.subtree_control` and
the available controller files. It creates `ozagia/` and a per-job
`ozagia/job-<id>`, enables `+memory +pids` (and `+io` when the parent exposes
it) where permitted, writes `memory.max` (bytes) and `pids.max`, and puts the
child in the group before exec (the parent also attaches the PID as a check).
It reads `memory.peak` (falling back to `memory.current`), `memory.events`
(`oom`, `oom_kill`), `pids.current`, `pids.max` and `io.stat`. `io.stat` is
summed over devices into `io_read_mb`/`io_write_mb` with
`io_source: "cgroup.io.stat"`; the record keeps the busiest few devices in a
bounded breakdown. A missing `io.stat` (io controller not delegated) is
`None`, not an error, and the watchdog uses `/proc/<pid>/io` instead. The
group is removed after the job's process group is empty; a group that cannot
be proven empty is left alone. The cgroup path is checked against
`<base>/ozagia/job-*` before removal, so a stale record cannot remove an
unrelated cgroup.

Delegation is required: the daemon's user must be able to create a child
cgroup and write the parent's `cgroup.subtree_control`. A systemd user session
usually delegates a slice to the user; a container or a plain login shell may
not. `ozagia-resources status --json` reports `enforcement.cgroups` with
`available`, `status`, `version`, `controllers`, `features` and the exact
`reason` when unavailable. Detection is read-only and never creates a group.

The remaining layers always apply:

- **Child rlimits (`preexec_fn`).** `memory_mb` sets `RLIMIT_AS`,
  `file_mb` sets `RLIMIT_FSIZE`, and an explicit `pids` limit can set
  `RLIMIT_NPROC` when `apply_child_limits` is called directly. Setting a limit
  never raises from the child path; unsupported or failed limits are reported
  in the function's report. The job wrapper leaves `RLIMIT_NPROC` to the
  cgroup or watchdog because that rlimit counts every process of the real UID,
  not the job's process group.
- **Watchdog.** After the spawn, a thread samples the job's process group
  (aggregate RSS, process count and `/proc/<pid>/io`) and the cwd filesystem
  every `sample_s` seconds. It keeps the peak RSS and attributed write total
  and, with a cgroup, also polls the cgroup's peak, OOM counters and
  `io.stat`; a cgroup value is preferred over the process-group sum. Exceeding
  `memory_mb`, `pids`, `wall_s` or `disk_mb` triggers one event. The disk
  budget is enforced against attributed writes when either source is readable
  (`kind: "disk_write"`). A fall below the disk reserve always stays
  `kind: "disk"`. Only when no attribution is readable does the watchdog use
  the free-space drop since its first sample (`kind: "disk"`); that fallback
  is host pressure, and another process writing to the same filesystem counts
  toward it. The reserve is `reserve_disk_gb`; `reserve_disk_pct` is an
  optional proportional addition and is 0 by default. The watchdog remains
  active with a cgroup because wall time and the process-group I/O fallback
  have no cgroup equivalent.
- **Verified groups only.** The watchdog's verifier checks that the record is
  this daemon's, still running, not marked `group_gone`, has an unreaped
  leader (so the pgid cannot be reused), and that `/proc` start times still
  identify the job's group. An unverified or stale group is recorded as
  `not_signalled`; no signal is sent.
- **Kill order.** SIGTERM to the verified process group,
  `RESOURCE_KILL_GRACE_S` (3 s), then `cgroup.kill` (SIGKILL semantics for
  every process in the job's own cgroup) when available, otherwise SIGKILL to
  the verified group. The reaper keeps the normal exit/kill state (`killed`,
  `signal`, `exit_code`); enforcement does not overwrite it.
- **OOM hint.** The child gets a best-effort `/proc/self/oom_score_adj` of
  `+500`, so if the kernel OOM killer runs despite the limits it prefers
  ozagia's child over the user's shell or desktop. Writing the value can fail
  (for example in a restricted container); that is reported, not fatal.

The job record returned by `job_start`, `job_list`, `job_wait` and `job_log`
carries `limits`, `enforcement` (`cgroup+rlimit`, `rlimit+watch` or `off`),
`cgroup`, `admission`, `peak_memory_mb`, `peak_disk_write_mb`,
`disk_write_source` and `resource_events`. `disk_write_source` is
`cgroup.io.stat` when the job had a readable cgroup `io.stat`, `proc.io` when
only the process-group sum was readable, and `None` when neither was. A cgroup
value is preferred and its final counter is read before the group is removed,
so `peak_disk_write_mb` can be raised at finish. An event names its `kind`
(`memory`, `pids`, `wall`, `disk_write`, `disk`, `oom` or `unverified`), the
limit and observed value, the verifier result and the action (`sigterm`,
`sigkill`, `cgroup.kill`, `cgroup_oom` or `not_signalled`). `disk_write` means
the attributed write budget was exceeded and names its `source`; `disk` is
either the reserve breach or the fallback free-space delta.

## Feedback to the session

On a resource breach the daemon appends lines that start with
`[ozagia] resource:` to the job's log. They name the job, the breach and the
signal sent; `job_wait` and `job_log` return them with the rest of the new
output. The same event is in `resource_events` on the job record, so a session
that only reads the reply does not need to parse the log.

Daemon `stats` exposes `jobs_resource_killed`, `resource_events`,
`resource_peak_mb`, `cgroup_jobs`, `cgroup_fallbacks` and `cgroup_oom_kills` at
the top level, and the full job counters (including
`resource_peak_disk_write_mb`) in the nested `jobs` object. `job_events` still
reports job ends, not resource events; the job record and log are the resource
surfaces.

## Example refusal

```json
{
  "ok": false,
  "reason": "memory.request",
  "requested": {"memory_mb": 4096, "disk_mb": 4096},
  "available": {"memory_mb": 1200, "disk_free_gb": 12.4},
  "expected": {"memory_mb": 3800, "samples": 14, "confidence": "medium"},
  "shortfall": {"memory_mb": 2896, "disk_mb": 0},
  "retry_after_s": 90,
  "suggestions": ["stop or finish another job", "retry in about 90 s"]
}
```

## Limits

- **Linux is the rich path.** procfs, pressure and cgroup data are Linux-only.
  Other platforms report `unknown` for unavailable values; they do not invent
  numbers.
- **Attribution is bounded.** `status` can show the largest process groups, but
  it cannot prove which session started an arbitrary process. Jobs and services
  that ozagia starts carry verified process groups and can be attributed.
- **Disk writes are attributed where Linux exposes them.** Free space is
  exact. Per-job writes are exact for the job's cgroup (`io.stat`) or process
  group (`/proc/<pid>/io` when cgroups are absent or the io controller is not
  delegated), as the kernel counts bytes sent to storage. They are not retained
  blocks: a rewrite counts again, page-cache bytes may not be counted yet, and
  a process writing through a path ozagia cannot attribute is not counted. The
  watchdog's `disk_mb` check uses the attributed total, not the filesystem free
  space, whenever one is readable.
- **cgroups are an enforcement and accounting layer.** A per-job
  `memory.max`/`pids.max` only exists when cgroup v2 is mounted and delegated;
  the daemon reports `cgroup+rlimit` when it worked, and `rlimit+watch` with a
  `cgroup.reason` when it did not. There is no global cgroup tree cleanup: an
  empty `ozagia/job-*` group is removed after its job ends, and a populated one
  is left for the kernel and its owner. cgroup memory accounting counts
  resident memory charged to the group, including page cache; `memory.peak`
  can be higher than the watchdog's RSS sum. Disk write accounting is exact for
  the group when `io.stat` is available; there is no per-file attribution.
- **The free-space delta is host pressure.** When neither `io.stat` nor
  `/proc/<pid>/io` is readable, the watchdog still stops a job when free space
  drops by more than `disk_mb` since its first sample, or when the filesystem
  falls below the configured reserve. Those events keep `kind: "disk"` because
  another process on the filesystem can contribute; they are not proof that
  this job wrote the bytes. A reserve breach is always `kind: "disk"`.
- **`RLIMIT_NPROC` is not used by the job wrapper.** It is per real UID and
  would count unrelated processes of the same user. The cgroup's `pids.max`
  (or the watchdog's process-count check without one) is per job. The helper
  can set `RLIMIT_NPROC` when called directly, for callers that want the
  UID-wide semantics.
- **Memory is aggregate.** `RLIMIT_AS` is virtual address space; the watchdog's
  `peak_memory_mb` is the sum of the group's resident memory at a sample; a
  cgroup's `memory.peak` is the kernel's charged peak for the group. None is a
  per-process exact accounting.
- **Wall time is sampled.** A job with `wall_s` is stopped up to one
  `sample_s` late; it is not a kernel timer.
- **No system-wide OOM killer.** Only verified groups ozagia started are
  signalled. Processes outside ozagia are observed, never killed. There is no
  guarantee against a kernel OOM caused by other processes, only early refusal
  and the `oom_score_adj` hint.
- **No network or GPU metrics** in this build.
