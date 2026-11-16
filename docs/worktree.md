# Worktree, branch and policy checks

`ozagia-worktree` is a deterministic git check. It never commits, merges,
pushes, switches a branch or removes a worktree. It answers five questions:

1. Is this the expected worktree and branch?
2. Has the worktree drifted from its base or upstream?
3. Is there room on this filesystem?
4. Does the commit/merge/push policy pass?
5. Is a mutation safe to offer?

Every result names the git command behind it. No model is called.

## Commands

```text
ozagia-worktree list [--json]
ozagia-worktree status [PATH] [--json]
ozagia-worktree check [--base main] [--json]
ozagia-worktree policy [PATH] --for commit|merge|push [--json]
ozagia-worktree prune --dry-run [--json]
```

Exit status is `0` when no error-level finding is present, `1` otherwise, and
`2` for a usage or git error. Warnings and `info` findings do not fail the
command by themselves.

Examples:

```bash
ozagia-worktree check --root .
ozagia-worktree policy --for commit --message 'feat: add worktree checks'
ozagia-worktree policy --for merge --target main
ozagia-worktree policy --for push --force
```

## Expected worktrees

With no configuration, the main worktree expects the base branch (`main`) and
a sibling directory named `ozagia-wt/<name>` expects `feat/<name>`. An explicit
mapping wins:

```toml
# ozagia/worktrees.toml
[worktrees.main]
path = "."
branch = "main"
protected = true

[worktrees.working-set]
path = "../ozagia-wt/working-set"
branch = "feat/working-set"
base = "main"
allow_dirty = true
```

`path` is relative to the main worktree (or absolute). The check reports a
wrong branch, detached HEAD, missing path, stale/prunable registration,
locked worktree, in-progress merge/rebase/cherry-pick/revert/bisect, conflicts,
dirty/untracked paths, ahead/behind counts and tmpfs worktrees.

A detached HEAD is a `warn` for `check` (a verification worktree may be
intentionally detached); commit/merge/push policy still refuses it. Dirty is
`info` for `check`; it becomes blocking for `policy --for merge|push`.

## Policy

`ozagia/policies.toml` is optional. Defaults:

```toml
[commit]
protected = ["main"]
branch_pattern = "^feat/"
message_pattern = "^(feat|fix|docs|test|refactor|perf|build|ci|style|revert)(\\(.+\\))?: .+"
require_signed = false
staged_only = false
allow_merge_commits = false
allow_fixup = false
allow_protected = false

[merge]
target = "main"
require_clean = true
require_up_to_date = false
mode = "either"          # ff, merge or either
required_checks = []

[push]
remote = "origin"
protected = ["main"]
allow_force = false
allow_tags = false
require_upstream = false
```

Commit checks include protected branches, branch and message patterns, author
identity, signed commits when required, staged-only state, conflicts, fixup and
merge commits, and whitespace errors.

Merge checks include the target ref, source/target cleanliness, a conflict
preview with `git merge-tree`, up-to-date state, and required checks supplied
as `--check NAME=passed`.

Push checks include protected branches, the remote, upstream state, force and
tag flags, and whether the branch is checked out in more than one worktree.

## Filesystem awareness

Each worktree status includes the filesystem facts from
[resources](resources.md): mount, `fstype`, free space, free inodes and
`is_tmpfs`. A worktree under `/tmp` on tmpfs consumes memory rather than disk;
`ozagia-resources check --for worktree --path <path>` applies
`resources.worktrees.avoid_tmpfs` and the minimum-free-space policy before a
worktree is created.

## Limits

- Read-only: `prune` is only available with `--dry-run`.
- The conflict preview needs a git with `merge-tree --write-tree`; without it
  the result is `not_checked`, not a claim of safety.
- Host filesystems can change between checks. Every report carries
  `checked_at`; re-run before a merge or push.
- The policy is repository configuration, not a security boundary. It does not
  prevent a caller from running `git` directly.
