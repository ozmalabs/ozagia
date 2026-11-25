# `ozagia-worktree`

> **Generated file.** Built by `scripts/docs/generate_docs.py` from `src/ozagia/worktree_cli.py` (ozagia `0.1.5.dev0`). Do not edit by hand: change the source code and regenerate.

Source: `src/ozagia/worktree_cli.py` (`ozagia-worktree` entry point).

# `ozagia-worktree`

read-only worktree, branch, drift and policy checks

```
usage: ozagia-worktree [-h] {list,status,check,policy,prune} ...
```

## `ozagia-worktree list`

```
usage: ozagia-worktree list [-h] [--root ROOT] [--json]
```

| option | type | values | default | help |
|---|---|---|---|---|
| `--root` |  |  |  | — |
| `--json` |  |  |  | — |

## `ozagia-worktree status`

```
usage: ozagia-worktree status [-h] [--base BASE] [--config-dir CONFIG_DIR]
                              [--root ROOT] [--json]
                              [path]
```

| argument | type | values | required | help |
|---|---|---|---|---|
| `path` |  | ? | no | — |

| option | type | values | default | help |
|---|---|---|---|---|
| `--base` |  |  | `main` | — |
| `--config-dir` |  |  |  | — |
| `--root` |  |  |  | — |
| `--json` |  |  |  | — |

## `ozagia-worktree check`

```
usage: ozagia-worktree check [-h] [--base BASE] [--config-dir CONFIG_DIR]
                             [--root ROOT] [--json]
```

| option | type | values | default | help |
|---|---|---|---|---|
| `--base` |  |  | `main` | — |
| `--config-dir` |  |  |  | — |
| `--root` |  |  |  | — |
| `--json` |  |  |  | — |

## `ozagia-worktree policy`

```
usage: ozagia-worktree policy [-h] --for {commit,merge,push} [--base BASE]
                              [--target TARGET] [--message MESSAGE]
                              [--check NAME=passed|failed] [--force] [--tags]
                              [--allow-secret sha256:FINGERPRINT]
                              [--config-dir CONFIG_DIR] [--root ROOT] [--json]
                              [path]
```

| argument | type | values | required | help |
|---|---|---|---|---|
| `path` |  | ? | no | — |

| option | type | values | default | help |
|---|---|---|---|---|
| `--for` |  |  |  | — |
| `--base` |  |  | `main` | — |
| `--target` |  |  |  | — |
| `--message` |  |  |  | — |
| `--check` |  |  |  | — |
| `--force` |  |  |  | — |
| `--tags` |  |  |  | — |
| `--allow-secret` |  |  |  | allow one finding once by fingerprint; the decision is recorded |
| `--config-dir` |  |  |  | — |
| `--root` |  |  |  | — |
| `--json` |  |  |  | — |

## `ozagia-worktree prune`

```
usage: ozagia-worktree prune [-h] [--dry-run] [--root ROOT] [--json]
```

| option | type | values | default | help |
|---|---|---|---|---|
| `--dry-run` |  |  |  | required; this build never prunes |
| `--root` |  |  |  | — |
| `--json` |  |  |  | — |
