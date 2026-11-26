# Temporary-space improvements

Status: implemented A-C (2026-11-26), except the residual path-resolution
window. Follows [the prior-art survey](../research/temp_managers.md) and the
first `ozagia tmp` slice. Execution was split into three sequential
agent-sized stages so each stage could be reviewed and tested on its own.

## Goal

Make `ozagia tmp` honest about the space it reports, safe against live owners,
bounded by explicit budgets, and useful without a manual cleanup command.

## Non-goals

- Replacing `systemd-tmpfiles` or claiming multi-user hostile-tree hardening.
- A cross-platform parity claim for Windows/macOS locking before it is tested.
- Deleting unowned paths, caches or stores by name or age alone.
- A full reference-counted store; the registry remains a root set, not a
  content-addressed object database.

## Stage A — accounting and discovery

1. `walk_usage` returns:
   - `allocated_bytes` from `st_blocks * 512` (with an explicit fallback when
     the platform reports no blocks);
   - `apparent_bytes` from `st_size`;
   - `files` (regular files), `inodes` (unique `(st_dev, st_ino)` for regular
     files, directories and symlinks visited), `hardlinks` (duplicate links);
   - `partial`/`unknown` as today.
2. Registry entries store `bytes` as **allocated** space, plus
   `apparent_bytes`, `files`, `inodes` and `hardlinks`. Existing records are
   recomputed on read; docs state the semantic change.
3. `status` adds filesystem `total_inodes`/`free_inodes`/`inode_percent` and a
   warning below 10% free inodes (when the filesystem reports inode counts).
4. `list`/`status` accept `--sort allocated|apparent|files|inodes|age` and
   `status --top N` returns the largest entries.
5. Tests: sparse file, hard-linked pair, symlink, unreadable entry, partial
   walk, inode accounting, and a filesystem with synthetic inode pressure.

Acceptance: a sparse 1 GiB file reports small allocated bytes and large
apparent bytes; a hard-linked pair counts one inode and one allocation; a
directory of 900 empty files reports 900 inodes and minimal bytes.

## Stage B — leases, pinning and budgets

1. Lease:
   - `acquire_lease(root, id)` opens `<entry>/.ozagia-lease` with
     `O_NOFOLLOW`, takes `flock(LOCK_EX|LOCK_NB)`, and returns a context
     manager holding the descriptor.
   - `clean`, `gc` and `release(delete=True)` skip an entry whose lease is
     held, even when its TTL expired. The lock dies with the process, so a
     crash cannot leave a permanent lease.
   - The agent harness acquires a lease for a default run root and releases it
     after the run; explicit `--runs-dir` stays unmanaged.
2. Pin:
   - `pin(root, id, until=None)` / `unpin(root, id)`; `pinned` and optional
     `pinned_until` in the record.
   - `clean`/`gc` never remove a pinned entry; `status` reports the count.
   - CLI `ozagia tmp pin/unpin`.
3. Budgets:
   - `TempBudget` with `max_total_bytes`, `max_inodes`, `max_session_bytes`,
     `max_session_inodes`, `min_free_bytes`.
   - `create` checks the global and session budget and refuses with a typed
     `TempBudgetError`; a model tool receives a refusal, not an override.
   - `gc` enforces the budget in this order: released/expired, then `cache`,
     then oldest `last_used_at`; skip pinned and leased entries; stop when the
     budget is met.
   - CLI `tmp gc --max-total-bytes --max-inodes --max-session-bytes
     --min-free --apply`; environment overrides
     `OZAGIA_TMP_MAX_BYTES`, `OZAGIA_TMP_MAX_INODES`,
     `OZAGIA_TMP_MIN_FREE`.
4. Tests: held lease keeps an expired entry; releasing permits deletion;
   pinned entries survive; budget refuses; gc evicts in the documented order;
   session budget isolation; min-free does not delete protected/leased paths.

Acceptance: with a 1 MiB budget, a new 2 MiB entry is refused; with a TTL
expired but a held lease, `clean --apply` keeps it; a pinned expired entry
stays until unpinned.

## Stage C — lifecycle, cache semantics and hardening

1. `ozagia tmp run --reason TEXT [--kind K] [--ttl-s N] [--keep] -- CMD...`:
   - create an entry, set `TMPDIR`/`TMP`/`TEMP` and
     `OZAGIA_SCRATCH_ROOT` for the child, run it with `shell=False`, then
     release/delete on exit 0 unless `--keep`; keep on failure/timeout.
   - print the entry id/path and the exit status; no prompt or output capture.
2. Add `cache` to the managed kinds:
   - `create(kind="cache")` writes a `CACHEDIR.TAG`;
   - `gc` evicts cache entries before scratch/agent entries;
   - `touch` is the use-recency signal for LRU (the daemon/plugin may call it).
3. Discovery:
   - `tmp list --sort`, `tmp status --top`, and a short `tmp usage` view of
     allocated/apparent bytes and inodes by kind/session.
   - `audit` skips directories carrying a `CACHEDIR.TAG`; `clean-legacy`
     already only knows ozagia patterns, so a tagged cache is never a legacy
     deletion candidate.
4. Hardening:
   - re-check `st_dev`/`st_ino` and the marker id immediately before deletion;
   - open lease/lock files with `O_NOFOLLOW`;
   - document the residual path-resolution race and the intended `openat2`
     `RESOLVE_BENEATH|RESOLVE_NO_SYMLINKS` direction for a later slice;
   - keep the no-follow, no-cross-filesystem, bounded-scan rules.
5. Tests: `tmp run` success/failure/keep, environment isolation, `cache`
   ordering and tag, list/status sorting and top, tagged-cache audit skip,
   and the deletion recheck.

Acceptance: `tmp run -- sh -c 'printf ok > "$TMPDIR/probe"'` creates and
removes its workspace; a tagged cache is reported but never cleaned by the
legacy path; a symlink swapped after planning is refused at apply time.

## Sequencing

| Stage | Files | Depends on |
| --- | --- | --- |
| A | `core/temp_space.py`, `temp_cli.py`, tests, `docs/temp.md` | current |
| B | same core/CLI, harness lease hook, tests, docs | A |
| C | same core/CLI, daemon/plugin touch + cache, tests, docs | B |

Each stage runs focused tests and ruff. The full suite runs after each stage is
staged, and `REPO_SHAPE.md` is regenerated at commit time. No stage changes the
sealed policy that unowned, unmarked or protected paths are never deleted.
