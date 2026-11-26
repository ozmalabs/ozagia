# Managed temporary space

`ozagia tmp` gives temporary directories a provenance record and a safe
cleanup path.  It exists because `/tmp` is a poor default workspace for models
and tools: it is often a RAM-backed `tmpfs`, it disappears across reboots or
container restarts, and nothing records who created a path, why, for which
session, or how much space it uses.

The core API lives in `ozagia.core.temp_space`; `ozagia.temp_cli` is the
command surface.  Both use the standard library only and never import or
execute the target project.

## Concepts

A managed entry is a directory plus two records.

| Record | Location | Purpose |
| --- | --- | --- |
| marker | `<entry>/.ozagia-tmp.json` (mode 0600) | proves ownership after a restart |
| lease | `<entry>/.ozagia-lease` (mode 0600) | advisory live-owner lock (`flock`); consumed by cleanup, not a record |
| registry record | `<state dir>/tmp/registry/<id>.json` (mode 0600) | authoritative metadata |

The state directory follows `ozagia.config.state_dir`: `<root>/.ozagia/` unless
`OZAGIA_STATE_DIR` names another directory.  Registry directories are mode
0700 and records are written atomically (same-directory temporary file,
`fsync`, `os.replace`).  One record per id means concurrent creates need no
global lock.

An entry records:

* a random opaque `id` (`tmp_` plus 12 hex characters);
* an absolute `path` under an allowed temp root;
* `kind`: `scratch`, `agent`, `check`, `eval`, `engine`, `cache` or `other`;
* `session`: `{harness, id}` or `null` (never invented; `session_source` is
  `unknown` when absent);
* optional `model` and `run_id` provenance labels (bounded, stored in the
  registry record only; they are not session substitutes);
* a short `reason` label (at most 200 characters, no control characters);
* the owning repository `root`, when any;
* `created_at`, `last_used_at`, optional `ttl_s` and derived `expires_at`;
* `pinned` and optional `pinned_until` (an epoch second used only as metadata;
  the pin lasts until `unpin`);
* `pid`, `owned`, `state`, `bytes` (**allocated** space), `apparent_bytes`,
  `files`, `inodes`, `hardlinks`, `allocation`, `walk` and `tmpfs`.
  `leased` is a live probe, not a stored field.

States are `active`, `released`, `expired` and `missing`.

Usage accounting separates allocated space from apparent length:

* `bytes` is **allocated** space: `st_blocks * 512` for each regular-file inode.
  It was previously `st_size`, so old plans, JSON consumers and docs that
  compared "bytes" to apparent size see a semantic change.
* `apparent_bytes` is the sum of `st_size` for regular files. A sparse file can
  have a large apparent size and a small allocated size.
* `files` counts regular-file entries, including every hard link.
* `inodes` counts unique `(st_dev, st_ino)` values for regular files,
  directories and symlinks visited. The entry directory and its marker count;
  a directory of empty lock files therefore shows many inodes and little space.
* `hardlinks` counts duplicate regular-file links. A hard-linked pair is one
  inode and one allocation but two `files` entries.
* `allocation` is `blocks` when block counts were usable, or
  `apparent-fallback` when the platform did not report `st_blocks`. On POSIX a
  zero block count is valid: it means a fully sparse file, so it is still
  `blocks`. The fallback uses `st_size` for `bytes` rather than silently
  claiming exact allocation. For a missing (or deleted) path the usage fields
  are zero and `allocation` is `None`.

Symlinks are never followed and never contribute file bytes; their own inode
is counted. Special files are ignored for bytes, files and inodes. The walk never
crosses a filesystem boundary: a child on a different `st_dev` is skipped and
marks the result `partial`, `unknown` and `cross_device`. A bounded walk that
stops at its entry, time or depth budget, or that meets an unreadable entry,
reports `partial` and `unknown` rather than a silent zero. `walk` is `complete`
or `partial`. Registry records store the same fields; `bytes`, `apparent_bytes`,
`files`, `inodes`, `hardlinks` and `allocation` are recomputed on read, so a
record written by an older version still gets the new view.

`tmpfs` is `true`, `false` or `null`; it is detected from
`/proc/self/mountinfo` (falling back to `/proc/mounts`) and is `null` on
non-Linux hosts or when the mount table is unavailable.

The marker and registry contain provenance only.  They never contain file
contents, environment variables or secret material.  `reason` is a short
caller-supplied label; callers must keep prompts, source text and secrets out
of it.

## Roots

`<state dir>/tmp/workspaces/` holds `scratch`, `check`, `eval`, `engine`,
`cache` and `other` entries.  `<state dir>/tmp/agent/` is reserved for agent
run roots.  A caller may adopt a path only under the resolved `TMPDIR`,
`/tmp`, `/var/tmp` or a managed root.

## Commands

```console
$ ozagia tmp path
/home/me/project/.ozagia/tmp/workspaces/scratch

$ ozagia tmp create --kind scratch --reason "parse fixture dump"
/home/me/project/.ozagia/tmp/workspaces/scratch/tmp_8f2c1a9d4e6b

$ ozagia tmp create --reason "nightly check" --kind check --ttl-s 3600 --json
{ "entry": { "id": "tmp_...", "path": "...", "ttl_s": 3600, ... }, "ok": true }

$ ozagia tmp adopt /tmp/experiment --reason "external tool output"
tmp_1a2b3c4d5e6f  /tmp/experiment

$ ozagia tmp list --kind scratch --owned
tmp_8f2c1a9d4e6b  scratch  active   owned=yes  allocated=18240  apparent=9216  files=7  inodes=8  session=unknown  ...

$ ozagia tmp list --sort allocated
tmp_...  scratch  active   owned=yes  allocated=...  apparent=...  files=...  inodes=...  session=...  ...
tmp_...  check    active   owned=yes  allocated=...  apparent=...  files=...  inodes=...  session=...  ...

$ ozagia tmp status
entries=3  allocated=42100  apparent=30000  files=19  inodes=22  partial=0  unknown=0
registry: /home/me/project/.ozagia/tmp/registry
by kind:
  scratch  entries=2 allocated=... apparent=... files=... inodes=... owned=2 unowned=0
filesystem /tmp (tmpfs): total=8589934592 free=734003200 used=7855931392 (91.5%) inodes=12000/65536 (81.7% used)
warning: tmpfs above 80% used (91.5%)

$ ozagia tmp status --top 3
...the status above...
top 3 by allocated:
  allocated=... apparent=... files=... inodes=...  scratch  opencode:ses_...  parse fixture dump  ...

$ ozagia tmp clean --expired
plan: 1 candidate(s), 18240 allocated, 9216 apparent, 7 files, 8 inodes, 0 deleted, 2 kept
  delete tmp_...  scratch  18240 allocated  9216 apparent  parse fixture dump  ...

$ ozagia tmp clean --expired --apply
$ ozagia tmp pin tmp_... --until 2026-12-31T00:00:00Z
$ ozagia tmp unpin tmp_...
$ ozagia tmp touch tmp_...
$ ozagia tmp gc --max-total-bytes 1073741824 --min-free 268435456
$ ozagia tmp gc --apply --max-total-bytes 1073741824

$ ozagia tmp run --reason "probe" -- sh -c 'printf ok > "$TMPDIR/probe"'
tmp_...  /home/me/project/.ozagia/tmp/workspaces/scratch/tmp_...  exit=0  cleaned

$ ozagia tmp run --reason "inspect failure" --keep -- sh -c 'exit 2'
tmp_...  /home/me/project/.ozagia/tmp/workspaces/scratch/tmp_...  exit=2  kept (--keep)

$ ozagia tmp run --reason "cache warm" --kind cache -- make cache
```

`create` also accepts `--max-total-bytes`, `--max-inodes`,
`--max-session-bytes`, `--max-session-inodes` and `--min-free`.  The
environment variables `OZAGIA_TMP_MAX_BYTES`, `OZAGIA_TMP_MAX_INODES` and
`OZAGIA_TMP_MIN_FREE` fill the corresponding fields when a caller does not
supply a budget; the session fields have no environment override.  A refusal
is a typed `TempBudgetError` from the API and exit status 1 with `"code":
"budget"` in JSON.

`list --sort` accepts `allocated` (the default discovery order for size),
`apparent`, `files` and `inodes` (largest first), or `age` (least recently used
first: `last_used_at`, then `created_at`). With no `--sort`, entries keep the
historical created-at order. `status --top N` adds the N largest entries by
allocated bytes, with kind, session, reason, path, allocated and apparent bytes,
files and inodes. `status` itself is the usage view: totals, per-kind and
per-session buckets report allocated and apparent bytes, files and inodes.
Filesystem capacity includes `total_inodes`, `free_inodes` and `inode_percent`
(used inodes, like the byte percent), with a warning when free inodes fall below
10% on a filesystem that reports a nonzero inode count.

`audit` scans the allowed temp roots for conservative legacy ozagia names
without creating, rewriting or deleting anything:

```console
$ ozagia tmp audit
artifacts=...  registered=0  untracked=...  allocated=...  apparent=...  files=...  inodes=...  partial=0  truncated=no
oldest: ...s  /tmp/ozagia_agent/.../stdout.log
newest: ...s  /tmp/ozagia-eval-....sock.lock
  untracked   4096B 0f age=...s /tmp/ozagia-eval-....sock.lock  [ozagia-eval-*.sock.lock]
```

It matches only `ozagia_agent/*`, `ozagia-eval-*.sock.lock`,
`ozagia-eval-*.sock`, `oz-idle-*.sock.lock`, `oz-jobidle-*.sock.lock`,
`ozagia_check_*`, `ozagia-native-*`, `ozagia_eval/*`,
`ozagia_index_bench/*` and `ozagia_iac_bench/*`.  Each artifact reports its
allocated and apparent bytes, files, inodes, age and whether its exact path (or
a registered ancestor) is in the registry.  Audit never follows symlinks and
skips `/tmp/opencode` and `~/.local/share/opencode`.  Untracked artifacts are
only reported; `clean` still deletes only owned, marker-matched registry
entries.

`clean` is always a plan unless `--apply` is given.  Selection options:

* `--expired`: entries whose TTL has elapsed;
* `--older-than D`: entries unused for `D` (a bare number means days; `s`, `m`,
  `h` and `d` suffixes are accepted);
* `--max-total-bytes B`: delete the oldest eligible owned entries until the
  allocated total is at most `B`;
* `--id ID` (repeatable): restrict to named entries.

With no selection option, cleanup considers released, missing and TTL-expired
entries.  Active entries are only candidates through an explicit TTL expiry,
an age threshold or a total-size cap.

### Leases, pinning, budgets and `gc` (2026-11-26)

A **lease** is an advisory `flock(LOCK_EX|LOCK_NB)` on
`<entry>/.ozagia-lease`, taken with `O_NOFOLLOW` and mode 0600.  The lock dies
with the process, so a crash cannot leave a permanent lease.
`TS.acquire_lease(root, id)` returns a context manager; `lease_held(root, id)`
probes without creating or deleting the file.  `clean`, `gc` and
`release(..., delete=True)` keep a held entry even when its TTL expired, and
`status`/`list` report `leased: true/false`.  Leases are advisory to
cooperative owners: a process that never takes one is still governed by TTL
and the other cleanup rules.

A **pin** protects an entry from `clean` and `gc` until it is removed.  A
pinned entry survives an expired TTL; `pinned_until` is optional metadata and
does not expire the pin.  Re-registering or calling `touch` does not clear a
pin.  `status` reports the pinned count, and the CLI has
`ozagia tmp pin ID [--until EPOCH|ISO]` and `ozagia tmp unpin ID`.

A **budget** (`TS.TempBudget`) limits total allocated bytes and inodes, the
same limits scoped to one `{harness, id}` session, and free bytes on the
filesystem.  `create` checks the active registry after creating and walking
the new entry; a refusal raises `TempBudgetError` and rolls back the directory
it created.  Session totals are scoped to the same `{harness, id}` and are not
shared between different sessions.  The daemon/plugin create path receives the
same refusal; there is no model or plugin force override.

`ozagia tmp gc` is a budget-driven collector.  It is a dry run unless
`--apply` is given.  Selection order is released/expired first, then `cache`
kind, then oldest `last_used_at`, then largest.  It stops when the total
allocated bytes/inodes are under the budget; if `--min-free` is still violated
it continues in the same order.  `--older-than D` restricts candidates to
entries unused for `D`.  With no budget, `gc` selects released and expired
entries.  `gc --apply` deletes only owned, marker-matched, unpinned, unleased
entries; unowned, marker-mismatched, symlinked, missing, protected, pinned and
leased entries are reported as kept.  A per-session limit makes that session's
entries candidates in the same order.

A **cache entry** (`--kind cache`) writes a conventional `CACHEDIR.TAG` with
the `Signature: 8a477f597d28d172789f06886806bc55` first line, in mode 0644,
when `create` makes the directory.  Ordinary scratch entries never get a tag.
`gc` orders released/expired entries first, then `cache` entries, then the
oldest `last_used_at`, so a regenerable cache is evicted before scratch work.
`audit` and `clean-legacy` skip a candidate directory that carries the tag:
it is treated as a cache by convention, not as an untracked legacy artifact,
and the cache tree is not walked.

`ozagia tmp touch ID` moves `last_used_at` to now for LRU ordering.  It does not
change the entry's state, kind or usage, and it never clears a pin or releases a
lease.

`ozagia tmp run --reason TEXT [--kind K] [--ttl-s N] [--keep] -- CMD...` runs
one command inside a fresh managed entry.  It sets `TMPDIR`, `TMP`, `TEMP` and
`OZAGIA_SCRATCH_ROOT` to the entry path, acquires the entry's lease, and runs
`CMD` with `subprocess.run(..., shell=False)` and inherited stdin/stdout/stderr.
There is no shell, so arguments are passed literally.  Command output, prompts
and environment values are never captured or printed by ozagia.  On exit 0 the
lease is released and the entry is deleted (with the normal owned/marker/budget
checks) unless `--keep` is given.  A non-zero exit, timeout, signal,
`KeyboardInterrupt`, or a cleanup refusal keeps the entry and releases the
lease, with the reason in the output.  A budget refusal exits 1 and creates
nothing.  Plain mode prints the entry id/path and exit status; JSON adds
`exit_code`, `entry`, `kept`, `cleaned`, `reason` and `usage`.

`--json` is available on every subcommand.  JSON is deterministic except for
timestamps and paths.  `create` prints only the created path in plain mode so a
caller can use it directly.

Exit status is 0 for success, including a cleanup plan with no work, and 1 for
a refusal or error.

## Safety rules

* Paths are resolved with `Path.resolve(strict=False)` and must stay inside an
  allowed root.  `..` and symlinked escapes are rejected.
* `create` refuses an existing non-empty directory, a symlink, or a file.
* `adopt` is unowned by default.  Unowned entries are reported and sized but
  `clean` never deletes them.
* An ownership marker is written only when `adopt --owned --mark` is explicit,
  and never over an existing marker with a different id.
* Cleanup only deletes an owned entry whose marker id matches.
* A held lease or a pin keeps an entry through `clean`, `gc` and
  `release(..., delete=True)`, even when the TTL expired.  Leases are advisory
  to cooperative owners; TTL still governs an owner that never takes one.
* `gc --apply` only touches owned, marker-matched, unpinned, unleased entries;
  its dry run is the default.
* Cleanup refuses a symlink, the filesystem root, the repository root, the home
  directory, the state/registry/managed roots, and any path equal to an allowed
  root.
* Deletion never follows a symlink out of the tree; directory symlinks are
  unlinked, not descended into.
* At apply time `clean` and `gc` re-read the registry record and marker, and
  compare the directory's planned `(st_dev, st_ino)` identity with the current
  one immediately before deletion.  A path replaced by a symlink, a different
  inode or a non-directory is kept and reported as `changed`; the replacement
  is never followed.
* `clean-legacy` records the planned `(st_dev, st_ino)` for each artifact row
  and applies the same recheck before `unlink` or `rmtree`.  Older rows without
  an identity still go through the name, type, lock/socket and protected-path
  checks.
* A missing path is reported as `missing` and does not fail the whole run.
* Nothing is written into the repository outside its state directory.
* No network access, no target import and no target execution.

### Residual path-resolution race

The identity recheck narrows the window but does not close it.  Paths are still
resolved by name: another process with write access to a parent directory can
replace a planned path between the final `lstat` and the `unlink`/`rmtree`.
ozagia never follows a replacement symlink and refuses a changed inode, but a
rename into the same name in that last instant is a classic time-of-check to
time-of-use race.  The intended hardening direction is Linux `openat2` with
`RESOLVE_BENEATH | RESOLVE_NO_SYMLINKS`, walking and deleting relative to a
held directory descriptor so the name cannot be swapped underneath the
operation.  That needs a ctypes or external dependency and is deliberately a
later slice; this module stays standard-library only and does not claim
hostile-parent hardening.

## What is tracked and what is not

Tracked: identity, kind, session, reason, owning root, pid, ownership, TTL,
pin state (`pinned`, optional `pinned_until`), state, allocated and apparent
byte totals, file and inode counts, hard-link duplicates, walk
completeness/cross-device marks, tmpfs membership, and per-filesystem byte and
inode capacity. A lease is a live probe, not stored metadata. Not tracked: file
contents, environment variables, prompts, source text, credentials, or the
output of whatever ran in the directory.

### Accounting limits

* Hard links are deduplicated only within one walked tree. A link elsewhere on
  the filesystem is invisible, so deleting this tree may reclaim less than the
  reported allocated bytes.
* A nested mount is skipped and marks the result `partial`/`cross_device`; data
  hidden behind a mount shadow is not counted. A bind mount of the same
  filesystem keeps the same `st_dev` and is therefore walked, not detected as a
  boundary.
* Deleted-but-open files have no directory entry; they consume blocks that this
  walk cannot see.
* The `apparent-fallback` mode uses `st_size`, so it is an approximation, not
  an allocation measurement. On POSIX, zero blocks is not a fallback: a fully
  sparse file correctly reports zero allocated bytes and a large apparent
  size. A non-POSIX platform that reports zero for every non-empty file is
  treated as fallback because it cannot be distinguished from absent block
  accounting.
* Directory and symlink `st_size` values are not added to byte totals; those
  entries contribute inodes only.

## Not temp: external assets

Durable datasets, model weights/metadata, corpora, installed checkouts and
long-lived caches are **not** temp entries.  `ozagia tmp audit`, `clean`,
`clean-legacy` and `gc` only consider the known temp roots and the managed
registry; they must not adopt or delete an external asset.  Ocelot's own data
(for example a Wikipedia parquet set) is external/unowned and is never a temp
candidate.  See the
[external-assets plan](plans/2026-11-26_external_assets.md),
[the assets catalog](assets.md) and the
[prior-art survey](research/temp_managers.md).

`/tmp` remains ephemeral.  Use `ozagia tmp path` (or the `default_scratch_root`
API) to place work under the state directory when it must outlive a boot or a
container, and use `adopt` only for paths whose lifetime you do not control.

## API

```python
from ozagia.core import temp_space as TS

entry = TS.create(root, kind="scratch", reason="fixture dump", ttl_s=3600)
entry = TS.create(root, kind="agent", reason="agent run", model="m",
                  run_id="run-7")
entry = TS.create(root, kind="cache", reason="build cache")  # CACHEDIR.TAG
entry = TS.adopt(root, "/tmp/tool-output", reason="tool output", owned=True,
                 mark=True)
entry = TS.get(root, entry["id"])
entry = TS.touch(root, entry["id"])             # moves last_used_at only
entry = TS.pin(root, entry["id"], until=None)  # protect from clean/gc
entry = TS.unpin(root, entry["id"])
entries = TS.list_entries(root, kind="scratch", owned=True)
entries = TS.list_entries(root, sort="allocated")  # or apparent/files/inodes/age
report = TS.status(root)                        # totals, buckets, filesystems
report = TS.status(root, top=5)                 # plus the 5 largest by allocated
audit = TS.audit(root)                          # read-only legacy scan
with TS.acquire_lease(root, entry["id"]):
    ...                                         # live while the block runs
held = TS.lease_held(root, entry["id"])          # probe, no lock retained
entry = TS.release(root, entry["id"], delete=True)
entry = TS.create(root, reason="bounded", budget=TS.TempBudget(
    max_total_bytes=1 << 30, max_session_bytes=1 << 28))
plan = TS.clean(root, expired=True)             # dry run
done = TS.clean(root, expired=True, apply=True)
plan = TS.gc(root, budget=TS.TempBudget(max_total_bytes=1 << 30))
done = TS.gc(root, budget=TS.TempBudget(max_total_bytes=1 << 30), apply=True)
pruned = TS.prune_registry(root, apply=True)
```

`get`, `list_entries` and `status` are read-only; only `touch` moves
`last_used_at`.  `make_session(harness, session_id)` builds the `{harness, id}`
value or returns `None` when no id is known.  `suggest_path` returns a
non-created suggestion, and `default_scratch_root` returns the managed scratch
root.

## Daemon `tmp` op and the `ozagia_scratch` tool (offline-tested)

The graph daemon exposes the manager as the `tmp` operation so a harness can
create scratch without a shell or a caller-supplied path:

| Action | Writes? | Answer |
| --- | --- | --- |
| `preflight` | no | `{ok, id: null, path, exists: false}` |
| `create` | one `scratch` entry | `{ok, id, path, expires_at, bytes, files, tmpfs}` |
| `info` | no | `{ok, entry}` for one `entry_id` |
| `list` | no | `{ok, entries, count, truncated, limit}` |
| `status` | no | `{ok, ...temp_space.status(root)}` |

Here `bytes` is allocated space. `info` and `list` return the full entry view,
including `apparent_bytes`, `inodes`, `hardlinks` and `allocation`.

The answers above show the handler fields. On the socket the JSON envelope's
`id` is the request id, so the entry id also travels as `entry_id` (and is
`null` for `preflight`); clients keep `entry_id`.

`preflight` and `create` require `harness: "opencode"`, a session id of 1..128
safe characters, and a reason of 1..200 characters without control characters.
`ttl_s` is 60..7776000 seconds and defaults to seven days; the kind is fixed to
`scratch`. A caller never supplies a path: `path` (or any other unknown field)
is refused with a stable `code`. The daemon's root and state directory are
authoritative, so the entry is always under
`<state dir>/tmp/workspaces/scratch`.

The OpenCode server plugin registers `ozagia_scratch` for the same operation.
It takes only `{purpose, ttl_s?}`:

```jsonc
{"purpose": "parse fixture dump", "ttl_s": 3600}
```

The tool preflights, asserts the harness's **write** permission for the
returned path, then creates at that same path (the daemon recomputes the
suggestion; the caller never supplies it). It returns only `{ok, id, path,
expires_at, bytes, files}` where `bytes` is allocated space. It fails closed: no
session, a denied permission or a down daemon returns a typed
`refused`/`unavailable` result and creates nothing. There is **no `/tmp`
fallback**, and no file contents or prompt text
are returned or stored.

The MCP full surface adds only the read-only views `dev_tmp_list` and
`dev_tmp_status`; it deliberately exposes no create, write or delete tool.
Creation remains the plugin, harness or `ozagia tmp create` path.

This daemon/plugin/MCP slice is covered by offline tests (focused daemon,
plugin and MCP suites). It has not been verified against a live OpenCode
Desktop session; no model or host is called by those tests.

## Agent and eval integration (2026-11-26)

The agent harness and the eval harness now use the registry. What is recorded,
and what is not:

* **Registered:** the default `ozagia agent` run root is adopted (`agent`
  kind, reason `opencode agent run`, `run_id`, optional model, `owned` and
  marked, 7-day TTL by default). A separate scratch workspace under
  `<state dir>/tmp/workspaces/agent/<run>` is created as its own owned entry
  (reason `agent scratch workspace`) and exposed as `OZAGIA_SCRATCH_ROOT`.
  Eval run roots that `new_run_root` creates are adopted (`eval` kind, run
  id, owned and marked, 7-day TTL). A default `GraphSession` log directory
  and the native harness's default log directory are created as managed
  `scratch`/`agent` entries; a caller-supplied `log_dir` is left alone.
* **Adopted vs created:** an agent or eval run root already exists when it is
  adopted, and it is marked only because the harness passes `owned=True,
  mark=True`. Scratch workspaces and default log directories are created as
  new entries. The design's old `/tmp` artifacts are neither: they are only
  *reported* by `ozagia tmp audit`, and this integration does not retroactively
  register or delete them.
* **Leased:** the OpenCode agent harness acquires a lease on its default
  managed run root right after registration and releases it after the run,
  before `--clean-logs` can delete. An explicit `--runs-dir` stays unmanaged
  and unleleased. The graph-session log directory and the model/plugin
  `ozagia_scratch` tool create ordinary entries without a lease; TTL, `touch`
  and the explicit `release`/`clean` paths remain their policy.
* **Never deleted automatically:** nothing is deleted merely because it is
  registered. TTL expiry only makes an entry eligible for
  `ozagia tmp clean --expired`. `ozagia agent --clean-logs` is the one
  explicit deletion path: on a successful run, with no kept clone, it calls
  `release(..., delete=True)` for the owned run root and scratch workspace.
  Failures, timeouts and kept clones keep them.
* Logs under `/tmp/ozagia_agent` remain ephemeral. Registration adds
  provenance and a cleanup path; it does not make them durable. Use
  `OZAGIA_SCRATCH_ROOT` (under the state directory) for files that must
  outlive a reboot, and `ozagia tmp release <id> --delete` to reclaim a run
  root by hand.

The harness also removes a daemon's `<socket>.lock` after the socket, using
`ozagia.daemon.autostart.release_lock`: it takes the lock first and leaves it
in place when a live process still holds it. This closes the per-run
`ozagia-eval-*.sock.lock` leak for daemon stop, start failure and cancellation
paths; previously a stopped daemon removed only the socket.

Explicit `--runs-dir` roots are deliberately unmanaged: they are not
registered under the source state directory and no managed scratch workspace
is created. The old behavior of keeping a run's own scratch under
`<run root>/scratch` is preserved.

## Observed host leak (2026-11-26)

On the development host, `/tmp` is a 16 GB tmpfs at roughly 1.4 GB used.  An
`ozagia tmp audit` run reported, approximately:

* 876 `ozagia-eval-*.sock.lock` files and 3 stale `ozagia-eval-*.sock` files;
* 74 `oz-idle-*.sock.lock` / `oz-jobidle-*.sock.lock` files;
* 58 MB under `ozagia_agent/` (agent logs).

These are untracked legacy artifacts: audit surfaces them, and this slice does
not delete them.  They are evidence for a future adoption/cleanup pass, not
managed entries.

## Legacy cleanup (`clean-legacy`, 2026-11-26)

`ozagia tmp clean-legacy` is a conservative, one-way migration path for the
legacy artifacts that predate the registry.  It is deliberately narrower than
`clean`:

```console
$ ozagia tmp clean-legacy [--apply] [--older-than D] [--include-caches] [--json]
```

The default is a plan; nothing is deleted without `--apply`.  The threshold is
one day (`86400` seconds); `--older-than` accepts a bare day count or an `s`,
`m`, `h` or `d` suffix.

Eligible by default, only when older than the threshold:

| Pattern | What is eligible | Extra check |
| --- | --- | --- |
| `ozagia-eval-*.sock.lock` | regular file | `flock(LOCK_EX\|LOCK_NB)`; a held lock is kept |
| `oz-idle-*.sock.lock` | regular file | same lock check |
| `oz-jobidle-*.sock.lock` | regular file | same lock check |
| `ozagia-eval-*.sock` | regular file or socket node | short connect; a live or unknown socket is kept |
| `ozagia_agent/*` | directory | protected-path guard |
| `ozagia_eval/*` | directory | protected-path guard |
| `ozagia_check_*` | directory | protected-path guard |
| `ozagia-native-*` | directory | protected-path guard |

`ozagia_index_bench/*` and `ozagia_iac_bench/*` are **not** eligible by default.
They are benchmark cache roots: usually regenerable, but one can contain a
report a user chose to keep, so they are only considered with
`--include-caches`.

The command only looks at paths that `ozagia tmp audit` already reports by a
known ozagia name pattern.  It never glob-deletes arbitrary temp files, never
looks at `/tmp/opencode` or `~/.local/share/opencode`, and never touches an
unknown name.  It also keeps:

* a path registered in the temp registry, and any path under a registered
  ancestor (the normal `clean` owns those entries);
* a candidate directory that carries a `CACHEDIR.TAG`; audit skips it before
  it becomes a row, and the decision/recheck also keeps it as `cache-tag`;
* a planned row whose `(st_dev, st_ino)` identity changed before deletion
  (reported as `changed`; the replacement is not followed);
* symlinks, non-regular lock files, and files where a directory pattern is
  expected;
* the filesystem root, repository root, home directory, allowed roots, managed
  roots, the state directory and `.ozagia/tmp` (reusing the protected-path
  helpers).

Deletion is best effort per artifact: one failure is reported as
`delete-failed` and the remaining planned artifacts are still attempted.
`clean-legacy` never writes a registry record.  This is cleanup, not adoption;
a path that should be kept and tracked belongs in `ozagia tmp adopt`.

A plan or a run with no work exits 0.  A refusal, or an apply run with a
deletion error, exits 1.  `--json` reports totals (`artifacts`, `candidates`,
`deleted`, `kept`, `bytes`, `files`, `errors`, `truncated`), per-artifact
`delete`/`keep` rows with the reason, the deleted paths, warnings, and the scan
and excluded roots.

The motivating evidence is the observed host leak: the 2026-11-26 `audit`
reported 959 untracked artifacts (about 60 MB) — 876
`ozagia-eval-*.sock.lock` files, 3 stale `ozagia-eval-*.sock` sockets, 74
`oz-idle-*`/`oz-jobidle-*` lock files and 6 `ozagia_agent/*` run roots.  That
audit is why the command exists; it is not a promise that every one of those
paths will be deleted.  Recent, held, live, registered, protected, cache and
unknown paths deliberately remain.

## Prior art

[Prior art for temporary-space management](research/temp_managers.md) surveys
the systems this design borrows from:

- **systemd-tmpfiles / tmpwatch / tmpreaper** — age from mtime/atime/ctime,
  explicit `x`/`X` excludes, `flock` liveness, `--test`, no-follow and
  no-cross-filesystem deletion;
- **systemd unit directories** — runtime state tied to a service lifetime, and
  separate state/cache/logs directories;
- **OpenCode tool-output** — bounded model-visible output with a temporary
  spill file and a retention period;
- **ccache/sccache, Bazel, BuildKit, Gradle, package managers and Nix** —
  size ceilings, LRU/use-recency, ordered GC policies, reference-aware
  mark-and-sweep and per-manager cleanup;
- **restic/borg and journald** — explicit retention policies and separate
  cheap-forget from expensive-prune/compact;
- **ncdu/agedu** — allocated versus apparent size, hard links, inodes and
  age-by-size discovery.

The survey's main remaining gaps for this implementation are recorded there:
leases and per-session size budgets now exist, while cleanup is still an
explicit command rather than an idle/end-of-session hook.  Accounting reports
allocated and apparent bytes, files, inodes and free-inode pressure.  Those are
design inputs, not claims about the current code.
The follow-up [improvements plan](plans/2026-11-26_temp_space_improvements.md)
sequences the work: accounting and discovery, then leases/pinning/budgets,
then lifecycle/cache/hardening.
