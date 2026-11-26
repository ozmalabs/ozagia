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
| registry record | `<state dir>/tmp/registry/<id>.json` (mode 0600) | authoritative metadata |

The state directory follows `ozagia.config.state_dir`: `<root>/.ozagia/` unless
`OZAGIA_STATE_DIR` names another directory.  Registry directories are mode
0700 and records are written atomically (same-directory temporary file,
`fsync`, `os.replace`).  One record per id means concurrent creates need no
global lock.

An entry records:

* a random opaque `id` (`tmp_` plus 12 hex characters);
* an absolute `path` under an allowed temp root;
* `kind`: `scratch`, `agent`, `check`, `eval`, `engine` or `other`;
* `session`: `{harness, id}` or `null` (never invented; `session_source` is
  `unknown` when absent);
* optional `model` and `run_id` provenance labels (bounded, stored in the
  registry record only; they are not session substitutes);
* a short `reason` label (at most 200 characters, no control characters);
* the owning repository `root`, when any;
* `created_at`, `last_used_at`, optional `ttl_s` and derived `expires_at`;
* `pid`, `owned`, `state`, `bytes`, `files`, `walk` and `tmpfs`.

States are `active`, `released`, `expired` and `missing`.  `walk` is `complete`
or `partial`: a bounded walk that stops at its entry, time or depth budget, or
that meets an unreadable entry, reports `partial` and `unknown` rather than a
silent zero.  `tmpfs` is `true`, `false` or `null`; it is detected from
`/proc/self/mountinfo` (falling back to `/proc/mounts`) and is `null` on
non-Linux hosts or when the mount table is unavailable.

The marker and registry contain provenance only.  They never contain file
contents, environment variables or secret material.  `reason` is a short
caller-supplied label; callers must keep prompts, source text and secrets out
of it.

## Roots

`<state dir>/tmp/workspaces/` holds `scratch`, `check`, `eval`, `engine` and
`other` entries.  `<state dir>/tmp/agent/` is reserved for agent run roots.  A
caller may adopt a path only under the resolved `TMPDIR`, `/tmp`, `/var/tmp` or
a managed root.

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
tmp_8f2c1a9d4e6b  scratch  active   owned=yes  bytes=18240  files=7  session=unknown  ...

$ ozagia tmp status
entries=3  bytes=42100  files=19  partial=0  unknown=0
filesystem /tmp (tmpfs): total=8589934592 free=734003200 used=7855931392 (91.5%)
warning: tmpfs above 80% used (91.5%)

$ ozagia tmp clean --expired
plan: 1 candidate(s), 18240 bytes, 7 files, 0 deleted, 2 kept
  delete tmp_...  scratch  18240 bytes  parse fixture dump  ...

$ ozagia tmp clean --expired --apply
```

`audit` scans the allowed temp roots for conservative legacy ozagia names
without creating, rewriting or deleting anything:

```console
$ ozagia tmp audit
artifacts=...  registered=0  untracked=...  bytes=...  files=...  partial=0  truncated=no
oldest: ...s  /tmp/ozagia_agent/.../stdout.log
newest: ...s  /tmp/ozagia-eval-....sock.lock
  untracked   0B 0f age=...s /tmp/ozagia-eval-....sock.lock  [ozagia-eval-*.sock.lock]
```

It matches only `ozagia_agent/*`, `ozagia-eval-*.sock.lock`,
`ozagia-eval-*.sock`, `oz-idle-*.sock.lock`, `oz-jobidle-*.sock.lock`,
`ozagia_check_*`, `ozagia-native-*`, `ozagia_eval/*`,
`ozagia_index_bench/*` and `ozagia_iac_bench/*`.  Each artifact reports its
bytes, files, age and whether its exact path (or a registered ancestor) is in
the registry.  Audit never follows symlinks and skips `/tmp/opencode` and
`~/.local/share/opencode`.  Untracked artifacts are only reported; `clean`
still deletes only owned, marker-matched registry entries.

`clean` is always a plan unless `--apply` is given.  Selection options:

* `--expired`: entries whose TTL has elapsed;
* `--older-than D`: entries unused for `D` (a bare number means days; `s`, `m`,
  `h` and `d` suffixes are accepted);
* `--max-total-bytes B`: delete the oldest eligible owned entries until the
  total is at most `B`;
* `--id ID` (repeatable): restrict to named entries.

With no selection option, cleanup considers released, missing and TTL-expired
entries.  Active entries are only candidates through an explicit TTL expiry,
an age threshold or a total-size cap.

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
* Cleanup refuses a symlink, the filesystem root, the repository root, the home
  directory, the state/registry/managed roots, and any path equal to an allowed
  root.
* Deletion never follows a symlink out of the tree; directory symlinks are
  unlinked, not descended into.
* A missing path is reported as `missing` and does not fail the whole run.
* Nothing is written into the repository outside its state directory.
* No network access, no target import and no target execution.

## What is tracked and what is not

Tracked: identity, kind, session, reason, owning root, pid, ownership, TTL,
state, byte/file totals, walk completeness, tmpfs membership, and per-filesystem
capacity.  Not tracked: file contents, environment variables, prompts, source
text, credentials, or the output of whatever ran in the directory.

`/tmp` remains ephemeral.  Use `ozagia tmp path` (or the `default_scratch_root`
API) to place work under the state directory when it must outlive a boot or a
container, and use `adopt` only for paths whose lifetime you do not control.

## API

```python
from ozagia.core import temp_space as TS

entry = TS.create(root, kind="scratch", reason="fixture dump", ttl_s=3600)
entry = TS.create(root, kind="agent", reason="agent run", model="m",
                  run_id="run-7")
entry = TS.adopt(root, "/tmp/tool-output", reason="tool output", owned=True,
                 mark=True)
entry = TS.get(root, entry["id"])
entry = TS.touch(root, entry["id"])             # moves last_used_at only
entries = TS.list_entries(root, kind="scratch", owned=True)
report = TS.status(root)
audit = TS.audit(root)                          # read-only legacy scan
entry = TS.release(root, entry["id"], delete=True)
plan = TS.clean(root, expired=True)             # dry run
done = TS.clean(root, expired=True, apply=True)
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
expires_at, bytes, files}`. It fails closed: no session, a denied permission or
a down daemon returns a typed `refused`/`unavailable` result and creates
nothing. There is **no `/tmp` fallback**, and no file contents or prompt text
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

The survey's main gaps for this implementation are recorded there: allocated
bytes and inodes are not yet reported, there is no lease or per-session size
budget, and cleanup is still an explicit command rather than an idle/end-of-
session hook.  Those are design inputs, not claims about the current code.
The follow-up [improvements plan](plans/2026-11-26_temp_space_improvements.md)
sequences the work: accounting and discovery, then leases/pinning/budgets,
then lifecycle/cache/hardening.
