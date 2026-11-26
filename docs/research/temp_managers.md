# Prior art for temporary-space management

Status: research note (2026-11-26). This surveys existing temp-file managers,
service/workspace lifecycles, cache garbage collectors and usage tools for
lessons that apply to `ozagia tmp`. It is a design input, not a claim that any
one system is directly reusable.

The central distinction is between four kinds of bytes that are often mixed
under "temporary":

| Kind | Lifetime | Example | Correct cleanup owner |
| --- | --- | --- | --- |
| ephemeral file | process or boot | prompt on stdin, socket, lock | the process, or an age/lock sweeper |
| scratch workspace | task/session | extracted archive, model scratch dir | the session/tool that created it |
| cache | rebuildable, shared | compiler objects, package downloads | the cache manager, by size/age/reference |
| durable state | varies | database, session record, report | never a temp sweeper |

Most failures come from applying one policy to the wrong kind. The systems
below are useful because they separate these cases explicitly.

## OS temp cleaners

### systemd-tmpfiles

`systemd-tmpfiles` is the closest existing analogue to `ozagia tmp`. Its
`tmpfiles.d` format is one line per path: type, path, mode, user, group, age,
argument. Relevant behaviours from the manual and the systemd "Using /tmp and
/var/tmp safely" guide:

- **Age is not just mtime.** By default the age check considers atime, mtime
  and ctime (except ctime for directories), and any sufficiently recent
  timestamp prevents deletion. `age-by` can restrict the test to particular
  timestamps, for example `bmA:1h` (mtime for files, atime for directories).
- **Age `0` means unconditional** on every `--clean`; a leading `~` cleans one
  level inside a directory but not its immediate children.
- **Explicit exclusion** uses `x` (recursively ignore) and `X` (ignore the
  path, not its contents). This is a declared policy, not a name heuristic.
- **`r`/`R` remove paths**, and `--remove` removes `D`/`R` contents. Neither
  follows symlinks.
- **Live processes can opt out with `flock`.** The aging algorithm takes an
  exclusive BSD lock before deleting; if any shared or exclusive lock is
  already held, the path and everything below it is skipped. Because the lock
  dies with the process, this is robust after a crash. The guide recommends
  `O_TMPFILE`/`memfd_create` first, then `flock` on a directory or file, then
  keeping atime fresh, then the sticky bit (regular files only), then `x`/`X`
  excludes.
- **`/tmp` defaults to 10 days, `/var/tmp` to 30 days.** The guide warns that
  long-running software must not assume its temp files are permanent.
- **Short-lived data belongs in `/run` or `$XDG_RUNTIME_DIR`,** not `/tmp`.
  `RuntimeDirectory=` creates a directory for the unit and removes it when the
  unit stops; `StateDirectory=`, `CacheDirectory=` and `LogsDirectory=` create
  the persistent variants and expose `$STATE_DIRECTORY`, `$CACHE_DIRECTORY`,
  `$LOGS_DIRECTORY`. `systemctl clean --what=cache|state|logs|runtime` is the
  explicit removal path.
- **`PrivateTmp=yes`** gives a service its own `/tmp` and `/var/tmp` through
  mount namespaces, reducing local DoS and cross-service interference.

Lessons for Ozagia:

- Age alone is weak. Explicit ownership plus a liveness lock is stronger.
- `flock` on a lease file is the cleanest "alive until the process dies"
  signal, and it composes with an age-0 policy.
- Separate the scratch workspace from `/tmp`; put runtime sockets in a short
  path that is still cleaned.
- Exclusion must be an explicit policy that survives name changes.

### tmpwatch and tmpreaper

`tmpwatch` (Fedora/RHEL) and `tmpreaper` (Debian) are the older age sweepers.
Their manuals are a catalogue of pitfalls:

- **Default is atime**, not mtime. `tmpwatch -m` and `tmpreaper --mtime`
  switch to mtime; tmpreaper can also require ctime (`--ctime`) because SMB/DOS
  copies can preserve old atime/mtime on a newly created file.
- Directory atime is unreliable: reading a directory updates its atime, so
  empty directories may never look old. `--dirmtime`/`--mtime-dir` fixes that.
- `--test` is the dry run. `--nodirs`, `--nosymlinks`, `--exclude`,
  `--exclude-pattern` and tmpreaper's `--protect` are the safety controls.
- They **do not follow symlinks**, do not switch filesystems, only remove
  regular files/empty directories (and sometimes unused sockets), and stop
  after a runtime limit because a hostile tree can otherwise keep the scan
  running indefinitely.
- tmpreaper's manual is blunt: it is sensitive to directory-change races and
  should exit on them; a simple `find … -exec rm` is acceptable only when the
  directory is not hostile.
- `--fuser`/`-s` is an optional liveness probe, but it is incomplete.

Lessons:

- Dry-run, no-follow, no-cross-filesystem, runtime caps and explicit excludes
  are baseline, not extras.
- Atime is a poor liveness signal; ctime is useful for copied-in files.
- Empty-directory detection needs a separate rule.

### macOS and Windows

- macOS clears `/tmp` at boot. Before Sonoma, `periodic daily` ran
  `110.clean-tmps` with a 3-day atime policy; since Sonoma, a launch daemon
  runs `/usr/libexec/tmp_cleaner` daily. Per-user `$TMPDIR` normally lives
  under `/var/folders/.../T/`, and its cleanup is less documented.
- Windows Storage Sense cleans user temporary files, the Recycle Bin and
  (optionally) Downloads, on a daily/weekly/monthly schedule or when disk
  space is low. It is user-scoped. Microsoft's documentation describes it as
  enabled by default under low-disk pressure in one place and off by default
  in another; either way, Windows may turn it on under pressure.

Lessons:

- "Temp" roots differ by platform; a portable manager must not assume `/tmp`.
- Low-free-space is a legitimate trigger in addition to age.
- User-scoped policy and per-user temp dirs avoid cross-user interference.

## Session and workspace lifecycles

### Gitpod and Coder

- Gitpod workspaces are ephemeral: `/workspace` is preserved across stop/start,
  the rest of the container is recreated; workspaces time out after inactivity
  (30 minutes by default in the cited material), are garbage-collected after
  about 14 days, and can be pinned. A dirty git tree postpones deletion.
- Coder separates ephemeral resources from persistent resources in the
  template. `RuntimeDirectory`-like resources are destroyed on stop; volumes
  persist. Dormant workspaces can be deleted automatically. Stable IDs
  (`workspace.id`) are used instead of mutable names so a rename does not
  destroy the volume.

Lessons:

- Distinguish "stopped" from "deleted"; do not equate inactivity with consent
  to destroy data.
- Pinning is a first-class escape hatch.
- Use immutable IDs, not names or reasons, for ownership.
- Dirty/uncommitted work should delay destructive cleanup.

### GitHub Actions self-hosted runners

The runner directory is a useful example of mixed lifecycles:

- `_work` is per-job checkout/build output and is safe to sweep.
- `_tool` is a shared tool cache; an age sweep deletes expensive Node/Python
  runtimes that setup actions expect to reuse.
- `_diag` is runner diagnostics.
- The recommended pattern is an ephemeral runner per job, caches outside the
  work tree, and a cleanup timer scoped to `_work` (plus `docker builder prune`
  with a time filter). A job-completed hook can clean deterministically.
- Workspace cleanup is not host cleanup: processes, installed tools, shared
  caches and credentials can survive.

Lessons:

- A single age sweep over a heterogeneous tree is a bug. Scope deletion to the
  ephemeral subtree.
- Shared caches need their own size/reference policy; age is wrong for them.
- A deterministic end-of-session hook is better than waiting for age, with age
  as the fallback.

### OpenCode tool-output

OpenCode's own `tool-output` store is instructive and cautionary:

- Oversized tool output is truncated in the model-visible result; the full text
  is written to a flat `tool_<id>` file and a bounded preview is the durable
  session record. The file is explicitly temporary.
- Current source has a 7-day retention and an hourly cleanup pass that filters
  `tool_*` names and removes files older than the retention. The managed output
  file is not the durable record; losing it must not turn a successful tool
  call into a failure.
- Public issue reports describe `tool-output` reaching tens of GB before
  cleanup existed, and `/tmp` spill files accumulating. Those are issue
  reports, not guarantees about every version, but they show the failure mode.

Lessons:

- Bound what the model sees; spill the rest to a file whose loss is acceptable.
- Retention is part of the storage contract, not an afterthought.
- Name-pattern cleanup is acceptable for a private flat store, but it needs a
  retention period and a "do not fail the operation" rule.

## Caches and build stores

### ccache and sccache

- ccache defaults to a 5 GiB size limit and no file-count limit. It cleans
  automatically after a write when a limit is exceeded, using approximate LRU
  by file mtime; a cache hit updates the mtime. `ccache --cleanup` forces a
  full recount and trim; `--evict-older-than` and `--evict-namespace` give
  age/namespace eviction.
- sccache uses a similar bounded local disk cache (10 GiB default) and LRU.
- Both distinguish "cache size" from "filesystem free space"; a size limit is
  a policy, not a reservation.

Lessons:

- A cache needs both a size ceiling and a use-recency update on hit.
- Approximate LRU is acceptable; exact global ordering is expensive.
- Namespaces let one physical cache serve several projects with scoped
  eviction.

### Bazel, BuildKit and Docker

- Bazel's disk cache GC supports `max_size` and `max_age`, runs in the
  background after an idle delay (default 5 minutes), and has a standalone GC
  tool. Repository caches have historically been age-only, which led to
  unbounded growth requests.
- BuildKit uses ordered GC policies with `reservedSpace`, `maxUsedSpace`,
  `minFreeSpace`, `keepDuration` and filters (`until`, `inuse`, `mutable`,
  `shared`, `private`, type). The default policy gives up cheap ephemeral
  records first, then old records, then unshared records over the limit, then
  any record over the limit. `reservedSpace` is a floor, `maxUsedSpace` a
  ceiling, `minFreeSpace` a target.
- A reported BuildKit bug shows the referential hazard: a manual prune that
  removes any record can make later builds miss unrelated cache entries. Cache
  deletion is not automatically semantically safe.

Lessons:

- Use a floor and a ceiling, not just "delete until under X".
- Order eviction by value/rebuild cost, not only age.
- Protect in-use and shared records.
- Prefer automatic idle GC to manual pruning; a partial manual prune can
  invalidate more than it removes.

### Gradle, package managers and Nix

- Gradle cleans caches periodically (at most daily) after daemon stop:
  released distributions 30 days, snapshots 7 days, downloaded resources 30
  days, created resources 7 days, local build cache 7 days, daemon logs 14
  days; all configurable. Caches are version-specific and old versions are
  cleaned when unused.
- uv (`cache clean`, `cache prune`, `--ci`), pip (`cache purge`), npm
  (`cache verify`/`clean --force`), pnpm (`store prune`), yarn (`cache clean`)
  and Maven (`dependency:purge-local-repository`) all provide a
  manager-owned cleanup. pnpm's store is content-addressed and shared by
  projects; `store prune` removes only unreferenced packages and warns that
  deleting the whole store is a cold-start event. uv blocks cache-modifying
  commands while other uv processes run, and recommends `uv cache prune --ci`
  at the end of CI.
- Nix is the clearest mark-and-sweep model: GC roots (profiles, symlinks,
  running processes) define live store paths; everything unreachable is
  collected. `--delete-older-than` removes old generations first,
  `--max-freed` stops after a budget, and `min-free`/`max-free` trigger GC
  under disk pressure. It moves dead paths to a trash directory before
  emptying it. `nix store delete` refuses paths that still have references.

Lessons:

- Reference-aware cleanup (mark-and-sweep) is safer than age for shared
  stores. A directory registry is a simple root set.
- Separate cheap "forget" from expensive "prune/compact" (restic/borg make the
  same distinction).
- Package-manager cleanup is manager-owned; a generic sweeper should not
  delete a shared store.
- End-of-CI pruning is a good deterministic hook.

### Restic, Borg and journald

- Restic `forget` removes snapshots but does not free space; `prune` does, and
  is expensive and locks the repository. `--dry-run` is available; retention
  policies (`--keep-*`) are explicit. Borg is similar and needs `compact`.
- journald uses `SystemMaxUse`, `SystemKeepFree`, `SystemMaxFileSize`,
  `SystemMaxFiles`, `MaxFileSec` and `MaxRetentionSec`; `journalctl
  --vacuum-size/time/files` is the supported manual path. Do not delete
  journal files with a generic temp sweeper.

Lessons:

- Retention policy should be explicit and separate from physical reclamation.
- A dry run and a plan/apply split are standard.
- Some stores are opaque and must be cleaned only through their owner.

## Safety and correctness practices

- **Create unnamed or immediately unlinked files.** `O_TMPFILE` (Linux) and
  `memfd_create` create files with no pathname, so they cannot be symlink
  targets and disappear when the last descriptor closes. `open` + `unlink`
  immediately is the portable variant.
- **Use descriptors, not re-resolved paths.** `mkstemp` returns a descriptor;
  re-opening by name reintroduces races. `openat2`'s `RESOLVE_BENEATH`,
  `RESOLVE_NO_SYMLINKS`, `RESOLVE_NO_MAGICLINKS` and `RESOLVE_NO_XDEV` let a
  process constrain lookup to a trusted directory. `O_NOFOLLOW` only protects
  the final component; `fstat` the opened object and check type, owner and
  device.
- **Avoid `mktemp`.** It returns a name without creating the file. Use
  `mkstemp`, `NamedTemporaryFile` or `TemporaryDirectory`.
- **`shutil.rmtree` caveat.** Python documents that cleanup is not robust
  against the tree being modified during removal, and on platforms where
  `shutil.rmtree.avoids_symlink_attacks` is false, a symlink race can remove
  files outside the tree. Do not follow directory symlinks; consider
  `rm -rf --one-file-system`-style boundaries.
- **Do not cross filesystems.** tmpwatch/tmpreaper avoid it; mount points can
  hide data or make a delete unexpectedly broad.
- **Respect live objects.** `flock`, socket connect probes, process checks and
  cgroup/PID ownership are all imperfect but better than age alone.
- **Bound the scan.** Entry, time and depth limits; tmpreaper stops after 55
  seconds to avoid a hostile tree keeping it alive.
- **Permissions and mount options.** 0700 directories and 0600 markers;
  `/tmp` is commonly sticky, and `noexec,nosuid,nodev` are common hardening
  options. Per-user `TMPDIR` or a private mount namespace avoids cross-user
  interference.

## Usage accounting

`du` and `df` answer different questions, and both have traps:

- `st_size` is apparent length; `st_blocks * 512` is allocated space. Sparse
  files can be huge in apparent size and tiny in allocated space; compressed
  or copy-on-write files can differ in the other direction.
- Hard links share one inode. `du` counts an inode once; naive sums can
  overcount. Deleting one link may reclaim nothing while another link exists.
- Inode counts matter independently of bytes: a directory with 879 tiny lock
  files can exhaust inodes or slow scans while using almost no space.
- Deleted-but-open files consume blocks but have no name; `du` cannot see them
  (`lsof +L1` can). Mount shadows can hide old data from a name walk while
  `df` still counts it.
- `ncdu` exposes allocated vs apparent size, hard-link handling and
  export/import scans; `agedu` correlates size with last-access age; both are
  better discovery tools than a single `du` total.
- `CACHEDIR.TAG` is a conventional marker that lets tools skip cache
  directories.

Lessons for `ozagia tmp`:

- Report **allocated bytes** and **apparent bytes** separately, plus files and
  inodes.
- Deduplicate hard links by `(st_dev, st_ino)` in a scan, or clearly label the
  number as an upper bound.
- Do not cross filesystem boundaries silently.
- Report `partial`/`unknown` when a bound or unreadable entry stops the walk.
- Watch inode pressure (`statvfs.f_files`/`f_ffree`), not just free bytes.

## Comparison

| System | Ownership | Age | Size/inodes | Live protection | Dry run | Provenance | Lifecycle |
| --- | --- | --- | --- | --- | --- | --- | --- |
| systemd-tmpfiles | path rules | mtime/atime/ctime, age-by | — | `flock`, sticky bit | yes | config line | boot/periodic |
| tmpwatch | directory + excludes | atime/mtime/ctime | — | optional `fuser` | `--test` | none | cron |
| tmpreaper | directory + protect | atime/mtime/ctime | — | `fuser`/protect | `--test` | none | cron |
| Storage Sense | user temp roots | days | low-disk trigger | in-use | settings | user | scheduled/pressure |
| systemd RuntimeDirectory | unit | — | — | unit stop | n/a | unit | service lifetime |
| OpenCode tool-output | private flat dir | 7 days | bounded preview | name filter | no | filename/session | hourly |
| ccache/sccache | cache dir | LRU mtime | size/files | — | `--dry-run` | namespace | on write/manual |
| Bazel disk cache | cache dir | max age | max size | `inuse` | GC tool | action key | idle GC |
| BuildKit | cache dir | keep duration | reserved/max/min-free | filters | prune | typed records | ordered GC |
| Gradle | cache dirs | 7/14/30 days | — | daemon stop | no | version | periodic |
| Nix | store + roots | generations | `--max-freed` | reachability | `--dry-run` | GC roots | manual/auto |
| restic/borg | repo | keep policy | max-unused | lock | yes | snapshots | forget/prune |
| **ozagia tmp** | **registry + marker** | **TTL + last-used** | **bytes/files, no inode cap yet** | **marker; no lease yet** | **default** | **session/reason/model/run** | **explicit/harness** |

## What Ozagia already does well

- Ownership is explicit: a per-entry marker plus an atomic registry record, not
  a name-pattern guess.
- Provenance is first-class: session `{harness, id}`, reason, model, run id,
  repo, pid, creation/use times.
- Cleanup is dry-run by default and only deletes owned, marker-matched entries;
  symlinks, protected roots and unowned paths are kept.
- `audit` and `clean-legacy` separate discovery from deletion and keep the
  legacy path conservative.
- Agent runs get an isolated per-run `TMPDIR` and a disk-backed scratch root;
  the model-facing `ozagia_scratch` tool is permission-gated and has no `/tmp`
  fallback.
- The daemon socket lock is removed with the socket, which fixed a real
  inode-leak class.

## Gaps worth addressing next

Prioritized from the survey:

1. **Accounting.** `walk_usage` currently sums `st_size` and does not dedupe
   hard links or count inodes. Add `allocated_bytes` (`st_blocks * 512`),
   `apparent_bytes`, `files`, `inodes`, and a `(st_dev, st_ino)` set for hard
   links. Report filesystem free bytes and free inodes in `status`.
2. **Budgets.** Add `--max-total-bytes`, `--max-inodes`, `--min-free` and a
   per-session/agent quota. Enforce at `create` and in `gc`; choose oldest
   `last_used_at` first, with `cache` entries before `scratch`/`agent` entries.
   This is the ccache/BuildKit/Nix lesson, and it directly addresses the
   lock-file inode leak.
3. **Leases.** Add a `lease` file that an owner `flock`s while it uses a
   directory. `clean` tries an exclusive lock and skips a held lease, even if
   the TTL has expired. This is systemd's strongest liveness mechanism and is
   crash-safe. Keep TTL as the fallback.
4. **Pinning.** Add `pin`/`unpin` or `keep_until`; `gc` skips pinned entries.
   Gitpod/Coder both need this escape hatch.
5. **Deterministic lifecycle.** Add `ozagia tmp run --reason -- <cmd>` that
   creates a workspace, sets `TMPDIR`, runs the command, and releases on
   success (keep on failure). Add an agent-end/daemon-idle GC hook with a
   cross-process lock, so cleanup does not wait for the next manual run.
6. **Cache semantics.** Add a `cache` kind with a size ceiling and LRU by
   `last_used_at`; let clients `touch` on use. Keep caches out of the
   ownership-delete path unless explicitly included.
7. **Security hardening.** Prefer `O_TMPFILE`/`memfd` for internal transient
   files; resolve through directory descriptors or `openat2` where available;
   re-check `st_dev`/`st_ino` immediately before unlink; keep the no-follow,
   no-cross-filesystem and bounded-scan rules. Treat Windows/macOS locking and
   temp roots as separate code paths.
8. **Discovery.** Add a top-N, age-by-size view and `CACHEDIR.TAG`/cache-marker
   support, so a user can see what is large and old before choosing a policy.

## Anti-patterns to avoid

- Age-only cleanup based on atime. Atime can be disabled, and directory atime
  changes during scans.
- `rm -rf /tmp/*` or name-glob deletion without ownership.
- Sweeping a shared tool cache or package store by age; it deletes expensive,
  still-referenced data.
- Deleting journal/store internals with a generic sweeper.
- Crossing filesystems or following symlinks during deletion.
- Ignoring hard links, sparse files and inodes when reporting "space freed".
- Treating "stopped" as "delete"; no pin, lease or dirty-work check.
- Combining an expensive prune/compact with a backup or a latency-sensitive
  operation.
- Trusting `mktemp` or re-opening a temporary file by name.

## Sources

- systemd, "Using /tmp/ and /var/tmp/ Safely":
  <https://systemd.io/TEMPORARY_DIRECTORIES/>
- `systemd-tmpfiles(8)` and `tmpfiles.d(5)`:
  <https://www.freedesktop.org/software/systemd/man/latest/systemd-tmpfiles.html>,
  <https://www.freedesktop.org/software/systemd/man/latest/tmpfiles.d.html>
- `systemd.exec(5)` directory settings:
  <https://www.freedesktop.org/software/systemd/man/latest/systemd.exec.html>
- `tmpwatch(8)`:
  <https://manpages.opensuse.org/Tumbleweed/tmpwatch/tmpwatch.8.en.html>
- `tmpreaper(8)`:
  <https://manpages.debian.org/trixie/tmpreaper/tmpreaper.8.en.html>
- macOS temp cleanup:
  <https://til.codeinthehole.com/posts/how-temp-files-are-removed-on-macos>,
  <https://superuser.com/questions/187071/in-macos-how-often-is-tmp-deleted>
- Windows Storage Sense:
  <https://learn.microsoft.com/en-us/windows/configuration/storage/storage-sense>
- ccache manual (cache size management):
  <https://ccache.dev/manual/latest.html>
- sccache configuration:
  <https://github.com/mozilla/sccache/blob/main/docs/Configuration.md>
- Bazel disk cache GC:
  <https://bazel.build/reference/command-line-reference>
- BuildKit GC:
  <https://docs.docker.com/build/cache/garbage-collection>,
  <https://docs.docker.com/build/buildkit/toml-configuration>
- Gradle cache cleanup:
  <https://docs.gradle.org/current/userguide/directory_layout.html>
- uv cache:
  <https://docs.astral.sh/uv/concepts/cache/>
- pnpm store:
  <https://pnpm.io/cli/store>
- Nix garbage collection:
  <https://nix.dev/manual/nix/stable/package-management/garbage-collection>,
  <https://nix.dev/manual/nix/stable/command-ref/nix-collect-garbage.html>
- restic forget/prune:
  <https://restic.readthedocs.io/en/stable/060_forget.html>
- Borg prune:
  <https://borgbackup.readthedocs.io/en/stable/usage/prune.html>
- journald retention:
  <https://www.freedesktop.org/software/systemd/man/journald.conf.html>
- OpenCode tool-output source and reports:
  <https://github.com/anomalyco/opencode/blob/dev/packages/core/src/tool-output-store.ts>,
  <https://github.com/anomalyco/opencode/issues/29694>
- Gitpod workspace lifecycle:
  <https://www.gitpod.io/docs/configure/workspaces/workspace-lifecycle>
- Coder workspace lifecycle:
  <https://coder.com/docs/user-guides/workspace-lifecycle>
- GitHub Actions self-hosted runners and cache management:
  <https://docs.github.com/en/actions/hosting-your-own-runners/managing-self-hosted-runners/autoscaling-with-self-hosted-runners>,
  <https://docs.github.com/en/actions/how-tos/manage-workflow-runs/manage-caches>
- `O_TMPFILE`, `openat2`, and symlink-race guidance:
  <https://man7.org/linux/man-pages/man2/open.2.html>,
  <https://man7.org/linux/man-pages/man2/openat2.2.html>
- Python `tempfile` and `shutil.rmtree` caveats:
  <https://docs.python.org/3/library/tempfile.html>,
  <https://docs.python.org/3/library/shutil.html>
- `ncdu` and `agedu`:
  <https://dev.yorhel.nl/ncdu/man>,
  <https://manpages.debian.org/bookworm/agedu/agedu.1.en.html>
