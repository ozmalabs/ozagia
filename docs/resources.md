# Host resources and admission checks

`ozagia-resources` observes the host and answers whether a job, task or
worktree is likely to fit. It is read-only: it does not start, signal or kill a
process. Enforcement (cgroups, rlimits, a watchdog and ozagia's own OOM) is a
later layer.

## What is observed

- **Memory:** total and available memory, swap, used percentage. Linux reads
  `/proc/meminfo`; other platforms use the optional `psutil` extra or
  `sysconf` and report `unknown` rather than guessing.
- **Disk:** for a path, the mount point, filesystem type, free bytes, free
  inodes and `is_tmpfs`. A worktree under `/tmp` on tmpfs consumes memory, not
  disk.
- **CPU/load:** one/five/fifteen-minute load averages and the CPU count.
- **Pressure:** Linux `/proc/pressure/{cpu,memory,io}` when available.
- **Process groups:** RSS per process group, and the largest groups by RSS.
  These are host-wide observations; they are not automatically attributed to a
  session. Only the executable name is reported, never the argument list,
  which may contain secrets.
- **History:** a bounded JSONL store under `.ozagia/resources/history.jsonl`
  with p50/p90/max memory, disk, duration and exit codes per key.

The history key is either a task class or `command_key(argv)`, which stores the
command head and a hash, never raw arguments. It is intended for expected-use
estimates, not for auditing commands.

## Commands

```text
ozagia-resources status [--path PATH] [--top N] [--json]
ozagia-resources check --for job|task|worktree [options] [--json]
ozagia-resources history [--key KEY] [--json]
ozagia-resources explain --for job|task|worktree [options]
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
watchdog = "auto"          # cgroup, rlimit, watch, off (later layer)
reserve_memory_pct = 10
reserve_disk_pct = 5
reserve_disk_gb = 1
history = true

[resources.limits]
job_memory_mb = 2048
job_disk_mb = 4096
job_pids = 256

[resources.admission]
min_free_memory_pct = 15
min_free_disk_pct = 10
min_free_disk_gb = 2
wait_max_s = 300

[resources.worktrees]
root = "../ozagia-wt"
min_free_gb = 5
avoid_tmpfs = true
```

The checks subtract the OS reserve from available memory and free disk. A host
already below the minimum is refused even for a small request. A request that
does not fit returns the shortfall, the expected p90 and suggestions.
`enabled = false` turns admission checks into a reported no-op; it does not
enable enforcement.

If another ozagia job is expected to free enough within the wait window,
`retry_after_s` is set and the caller can retry or wait. This build does not
wait silently.

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
- **Disk is filesystem-level.** Free space is exact; per-process or per-job
  disk usage is best-effort and is not claimed when writes cannot be
  attributed.
- **No enforcement yet.** This build refuses and reports; it does not kill.
  The watchdog, cgroup/rlimit limits and `resource_event` feedback are the
  next layer.
- **No network or GPU metrics** in this build.
